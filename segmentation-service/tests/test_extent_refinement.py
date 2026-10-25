from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from housing_segmentation.captures.acceptance import assess_geometry
from housing_segmentation.captures.extent import RefinedExtent, refine_extent
from housing_segmentation.captures.format import Capture, extract_archive, parse_manifest, matrix
from housing_segmentation.captures.fusion import fuse_depth, background_patch
from housing_segmentation.captures.geometry import object_local_frame, transform_points, anchor_depth_points, depth_to_image
from housing_segmentation.captures.masking import propagate_masks


@pytest.fixture(scope='module')
def phone_source(tmp_path_factory):
    fixture = Path(__file__).resolve().parents[2] / 'fixtures/phone_capture_e2e'
    manifest = json.loads((fixture / 'manifest.json').read_text())
    root = tmp_path_factory.mktemp('phone-seed-refinement')
    extract_archive(fixture / 'archive.zip', root, parse_manifest(manifest))
    return root, manifest, json.loads((fixture / 'ground_truth.json').read_text())


@pytest.mark.parametrize('seed_kind', ['tight', 'loose_into_table', 'undersized'])
@pytest.mark.parametrize('tilted', [False, True])
def test_seed_box_is_only_a_prior(phone_source, tmp_path, seed_kind, tilted):
    root, payload, truth = phone_source
    payload = deepcopy(payload)
    a2w = matrix(payload['anchor_to_world'])
    if tilted:
        angle = .31
        rotation = np.eye(4)
        rotation[:3, :3] = [[1, 0, 0], [0, np.cos(angle), -np.sin(angle)], [0, np.sin(angle), np.cos(angle)]]
        a2w = a2w @ rotation
    payload['anchor_to_world'] = a2w.reshape(-1, order='F').tolist()
    for frame in payload['frames']:
        frame['camera_to_anchor'] = (np.linalg.inv(a2w) @ matrix(frame['camera_to_world'])).reshape(-1, order='F').tolist()
    low, high = np.array(truth['object_min_world']), np.array(truth['object_max_world'])
    corners = np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
    corners = transform_points(corners, np.linalg.inv(a2w))
    lo, hi = corners.min(0) - .002, corners.max(0) + .002
    if seed_kind == 'loose_into_table':
        lo -= [.18, .25, .18]; hi += [.18, .08, .18]
    elif seed_kind == 'undersized':
        centre = (lo + hi) / 2
        lo, hi = centre + (lo-centre)*.55, centre + (hi-centre)*.55
    payload['seed']['bbox_anchor'] = {'min': lo.tolist(), 'max': hi.tolist()}
    capture = Capture(root, parse_manifest(payload))
    masks, masking = propagate_masks(capture, tmp_path / 'masks', lambda: None, backend='depth_bbox')
    extent = RefinedExtent.from_report(masking['extent_refinement'])
    object_mesh, fusion, object_points = fuse_depth(capture, masks, lambda: None, object_only=True, extent=extent)
    background, _, cloud = fuse_depth(capture, masks, lambda: None, object_only=False, extent=extent)
    background, _ = background_patch(capture, background, cloud, lambda: None, extent=extent)
    local, transform, dimensions = object_local_frame(object_mesh.vertices, capture.manifest.anchor_to_world)
    assert np.allclose(transform_points(local, transform), object_mesh.vertices)
    assert np.max(np.abs(dimensions - [.4, .3, .3])) < .02, dimensions
    assert not extent.on_support(object_points).any()
    assert len(extent.planes) >= 2
    result = assess_geometry(np.asarray(object_mesh.vertices), np.asarray(background.vertices), np.asarray(background.triangles),
                             capture.manifest.anchor_to_world, truth, object_to_anchor=transform)
    result.update(seed_kind=seed_kind, tilted_anchor=tilted, dimensions_m=dimensions.tolist(),
                  outside_seed_fraction=fusion['outside_seed_fraction'], outside_refined_fraction=fusion['outside_refined_fraction'])
    (tmp_path / 'metrics.json').write_text(json.dumps(result, indent=2))
    assert result['passes'], result
    assert np.allclose(a2w[:3, :3] @ transform[:3, 1], [0, 1, 0], atol=1e-6)
    if seed_kind == 'undersized':
        assert fusion['outside_seed_fraction'] > .25
        assert masking['extent_refinement']['seed_was_truncating']
    if seed_kind == 'loose_into_table':
        assert fusion['outside_seed_fraction'] < .01
    assert fusion['outside_refined_fraction'] < .01


def test_yaw_comes_from_rectangle_not_anchor():
    angle = .57
    a2w = np.eye(4)
    a2w[:3, :3] = [[np.cos(angle), 0, np.sin(angle)], [0, 1, 0], [-np.sin(angle), 0, np.cos(angle)]]
    world = np.array([[x, y, z] for x in (-.2, .2) for y in (.61, .91) for z in (-.15, .15)])
    anchor = transform_points(world, np.linalg.inv(a2w))
    local, pose, dimensions = object_local_frame(anchor, a2w.reshape(-1, order='F'))
    assert np.allclose(dimensions, [.4, .3, .3], atol=1e-6)
    assert np.allclose(transform_points(local, pose), anchor)
    assert np.allclose(a2w[:3, :3] @ pose[:3, 1], [0, 1, 0])


@pytest.mark.parametrize('seed_scale', [.55, 1., 2.])
def test_sam_depth_evidence_rejects_leaked_table(phone_source, seed_scale):
    import cv2
    from PIL import Image
    root, payload, truth = phone_source
    payload = deepcopy(payload)
    seed = payload['seed']['bbox_anchor']
    centre = (np.array(seed['min']) + seed['max']) / 2
    seed['min'] = (centre + (np.array(seed['min'])-centre) * seed_scale).tolist()
    seed['max'] = (centre + (np.array(seed['max'])-centre) * seed_scale).tolist()
    capture = Capture(root, parse_manifest(payload))
    masks = []
    for frame in capture.manifest.frames:
        if frame.index == capture.manifest.seed.frame_index:
            masks.append(np.array(Image.open(root / capture.manifest.seed.mask)) > 0)
            continue
        depth = capture.depth(frame)
        points = anchor_depth_points(frame, depth)
        world = transform_points(points.reshape(-1, 3), matrix(capture.manifest.anchor_to_world)).reshape(points.shape)
        selected = np.all((world >= np.array(truth['object_min_world']) - .001) & (world <= np.array(truth['object_max_world']) + .001), axis=-1)
        # Deliberately contaminated SAM-like mask includes a broad table patch.
        selected |= (np.abs(world[..., 1] - .61) < .004) & (np.abs(world[..., 0]) < .5) & (np.abs(world[..., 2]) < .45)
        yy, xx = np.indices((frame.height, frame.width), dtype=np.float32)
        sx, sy, tx, ty = frame.depth.to_image
        masks.append(cv2.remap(selected.astype(np.uint8), (xx+.5-tx)/sx-.5, (yy+.5-ty)/sy-.5, cv2.INTER_NEAREST) > 0)
    extent, report = refine_extent(capture, masks, lambda: None)
    object_cloud = []
    for frame, mask in zip(capture.manifest.frames, masks):
        x, y = depth_to_image(frame)
        points = anchor_depth_points(frame, capture.depth(frame))
        object_cloud.append(points[mask[y, x] & extent.contains(points) & ~extent.on_support(points)])
    world = transform_points(np.concatenate(object_cloud), matrix(capture.manifest.anchor_to_world))
    assert world[:, 1].min() > .624
    assert np.max(np.abs(world[:, 0])) < .205
    assert np.max(np.abs(world[:, 2])) < .155
    assert report['source'] == 'multi-view SAM-masked depth'
    assert len(report['support_planes']) >= 2


def test_collapsed_mps_video_retries_official_model_on_cpu(phone_source, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from PIL import Image
    import torch
    from housing_segmentation.captures import masking
    root, payload, _ = phone_source
    capture = Capture(root, parse_manifest(payload))
    approved = np.array(Image.open(root / capture.manifest.seed.mask)) > 0
    devices = []
    class Predictor:
        def __init__(self, device): self.device = device
        def init_state(self, **kwargs): return {}
        def add_new_mask(self, *args, **kwargs): pass
        def propagate_in_video(self, state, start_frame_idx, reverse=False, **kwargs):
            for i in (range(start_frame_idx, -1, -1) if reverse else range(start_frame_idx, 36)):
                values = np.where(approved, 10., -10.).astype(np.float32)
                if self.device == 'mps' and i != 0: values[:] = -1024
                yield i, [1], torch.from_numpy(values)[None, None]
    class Factory:
        @staticmethod
        def from_pretrained(model, *, device, **kwargs):
            assert model == 'facebook/sam2.1-hiera-tiny'
            devices.append(device)
            return Predictor(device)
    monkeypatch.setitem(sys.modules, 'sam2.sam2_video_predictor', SimpleNamespace(SAM2VideoPredictor=Factory))
    monkeypatch.setattr(torch.backends.mps, 'is_available', lambda: True)
    monkeypatch.setattr(torch.mps, 'empty_cache', lambda: None)
    monkeypatch.setattr(masking, 'refine_extent', lambda *args, **kwargs: (RefinedExtent(np.eye(3), np.full(3,-10), np.full(3,10), []), {}))
    _, report = masking.propagate_masks(capture, tmp_path, lambda: None)
    assert devices == ['mps', 'cpu']
    assert report['device'] == 'cpu'
    assert '35/35' in report['device_fallback_reason']
    assert report['requested_device'] == 'auto'
