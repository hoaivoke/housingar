"""CPU regressions from actual masked Pixel photogrammetry surfaces.

The cached native output is explicitly substituted for the hardware-dependent
Apple call. Repair, metric bounds, outline quality and fallback selection run.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import open3d as o3d
import pytest
from PIL import Image

from housing_segmentation.captures.format import Capture, Frame
from housing_segmentation.captures.image_geometry import (
    adaptive_depth,
    phone_images,
    reconstruct_images,
    silhouette_quality,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/real_reconstruction"


@pytest.mark.parametrize("name", ["black-chair", "second-chair", "wooden-chair"])
def test_real_photo_surface_keeps_upper_and_lower_geometry(name, tmp_path, monkeypatch):
    meta = json.loads((FIXTURES / (name + ".json")).read_text())
    with np.load(FIXTURES / (name + ".npz")) as data:
        mesh = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(data["vertices"]),
            o3d.utility.Vector3iVector(data["faces"]),
        )
        masks = np.unpackbits(data["masks"], axis=2)
    frames = [Frame.model_validate(f) for f in meta["frames"]]
    capture = Capture(
        tmp_path,
        SimpleNamespace(frames=frames, anchor_to_world=meta["anchor_to_world"]),
    )
    mask_dir = tmp_path / "masks"
    mask_dir.mkdir()
    for f, mask in zip(frames, masks):
        Image.fromarray(mask[:, : f.width] * 255).save(mask_dir / f"{f.index:04d}.png")
    from housing_segmentation.captures import object_capture

    monkeypatch.setattr(
        object_capture,
        "run_object_capture",
        lambda *a, **kw: (mesh, meta["registration"]),
    )
    repaired, report = reconstruct_images(
        capture,
        mask_dir,
        tmp_path / "out",
        lambda: None,
        point_anchor=meta["point_anchor"],
    )
    assert report["method"] == "apple_masked_photogrammetry"
    assert report["raw_depth_used_for_surface"] is False
    assert repaired.is_watertight() and not repaired.is_self_intersecting()
    assert len(repaired.cluster_connected_triangles()[1]) == 1
    assert len(repaired.triangles) <= 50000
    from housing_segmentation.captures.geometry import object_local_frame

    dimensions = object_local_frame(repaired.vertices, meta["anchor_to_world"])[2]
    assert np.max(np.abs(dimensions - meta["expected_dimensions_m"])) < 0.02
    q = report["silhouette_validation"]
    assert q["median"]["recall"] > 0.9
    assert q["median"]["lower_recall"] > 0.7
    assert q["median"]["upper_recall"] > 0.9
    assert not report["geometry_completeness"]["low_quality"]
    if name == "black-chair":
        assert "boundary_repair_rejected" in report["stages"]
        assert (
            len(report["stages"]["surface_completion"]["mask_supported_bridges"]) >= 1
        )


def test_depth_fallback_uses_real_resolution_and_tap_component(monkeypatch):
    from housing_segmentation.captures import fusion, image_geometry

    k = SimpleNamespace(fx=110, fy=110)
    frame = SimpleNamespace(
        width=160,
        height=90,
        depth=SimpleNamespace(
            width=160, height=90, intrinsics=k, to_image=[1, 1, 0, 0]
        ),
    )
    confidence = np.full((90, 160), 255, np.uint8)
    confidence[:, :80] = 100
    capture = SimpleNamespace(
        manifest=SimpleNamespace(frames=[frame]),
        depth=lambda f: np.full((90, 160), 2000, np.uint16),
        confidence=lambda f: confidence,
    )
    big = o3d.geometry.TriangleMesh.create_box(0.5, 0.5, 0.5)
    small = o3d.geometry.TriangleMesh.create_box(0.1, 0.1, 0.1).translate([1, 0, 0])
    merged = big + small
    called = {}

    def fake_fusion(*args, **kwargs):
        called.update(kwargs)
        return merged, {}, None

    monkeypatch.setattr(fusion, "fuse_depth", fake_fusion)
    monkeypatch.setattr(
        image_geometry, "photo_volume_repair", lambda mesh, *args, **kwargs: (mesh, {})
    )
    result, report = adaptive_depth(
        capture,
        [np.ones((90, 160), bool)],
        lambda: None,
        extent=SimpleNamespace(planes=[]),
        point_anchor=[1.05, 0.05, 0.05],
    )
    assert called["minimum_confidence"] == 192
    assert called["voxel"] >= 0.027
    assert report["components_before_selection"] == 2
    assert len(result.cluster_connected_triangles()[1]) == 1
    assert np.asarray(result.vertices)[:, 0].min() >= 1


def test_missing_confident_depth_fails_clearly():
    frame = SimpleNamespace(
        width=160,
        height=90,
        depth=SimpleNamespace(width=160, height=90, to_image=[1, 1, 0, 0]),
    )
    capture = SimpleNamespace(
        manifest=SimpleNamespace(frames=[frame]),
        depth=lambda f: np.ones((90, 160), np.uint16),
        confidence=lambda f: np.zeros((90, 160), np.uint8),
    )
    with pytest.raises(ValueError, match="high-confidence"):
        adaptive_depth(
            capture,
            [np.ones((90, 160), bool)],
            lambda: None,
            extent=SimpleNamespace(planes=[]),
        )


def test_phone_resolution_selector_keeps_synthetic_depth_path():
    def capture(width, depth):
        return SimpleNamespace(
            manifest=SimpleNamespace(
                frames=[
                    SimpleNamespace(width=width, depth=SimpleNamespace(width=depth))
                ]
            )
        )

    assert phone_images(capture(1920, 160))
    assert not phone_images(capture(320, 160))


def test_outline_quality_flags_a_chair_with_half_its_height_missing():
    meta = json.loads((FIXTURES / "wooden-chair.json").read_text())
    with np.load(FIXTURES / "wooden-chair.npz") as data:
        vertices = data["vertices"].copy()
        faces = data["faces"]
        masks = np.unpackbits(data["masks"], axis=2) > 0
    from housing_segmentation.captures.geometry import gravity_frame

    up = gravity_frame(meta["anchor_to_world"])[:, 1]
    height = vertices @ up
    vertices += (0.45 * (height - height.min()) + height.min() - height)[:, None] * up
    squashed = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(vertices), o3d.utility.Vector3iVector(faces)
    )
    capture = Capture(
        Path("."),
        SimpleNamespace(
            frames=[Frame.model_validate(f) for f in meta["frames"]],
            anchor_to_world=meta["anchor_to_world"],
        ),
    )
    quality = silhouette_quality(capture, masks, squashed, lambda: None)
    assert quality["low_quality"]
    assert quality["median"]["upper_recall"] < 0.5


@pytest.mark.parametrize("succeeds", [True, False])
def test_real_scan_routes_photos_even_when_depth_refinement_failed(
    succeeds, tmp_path, monkeypatch
):
    from housing_segmentation.scans import pipeline, tapped
    from housing_segmentation.scans.discovery import Candidate

    meta = json.loads((FIXTURES / "black-chair.json").read_text())
    frames = [Frame.model_validate(f) for f in meta["frames"]]
    manifest = SimpleNamespace(
        frames=frames,
        anchor_to_world=meta["anchor_to_world"],
        objects=[
            SimpleNamespace(object_id="obj_01", point_anchor=meta["point_anchor"])
        ],
        scan_id="regression",
    )
    capture = Capture(tmp_path, manifest)
    monkeypatch.setattr(pipeline.Scan, "load", lambda root: capture)
    monkeypatch.setattr(
        Capture, "image", lambda self, f: np.zeros((f.height, f.width, 3), np.uint8)
    )
    mask = np.ones((frames[0].height, frames[0].width), bool)
    info = {
        "best_frame": 0,
        "thumbnail_mask": mask,
        "failure_reason": "No reliable raw depth",
        "extent": None,
        "masks": [mask] * len(frames),
        "appearance_indexes": [0],
        "views": 12,
        "coverage_deg": 240.0,
        "validated_depth_points": 0,
        "validated_cloud": np.empty((0, 3)),
    }
    candidate = Candidate(
        "obj_01",
        np.array([[0, 0, 0], [0.1, 0.2, 0.1]]),
        parent_id=None,
        excluded_planes=[],
    )
    monkeypatch.setattr(
        tapped, "tapped_candidates", lambda *a, **kw: ([candidate], [info])
    )
    monkeypatch.setattr(pipeline, "thumbnail", lambda *a: None)
    monkeypatch.setattr(
        pipeline,
        "fuse_depth",
        lambda *a, **kw: pytest.fail("Image main path must not fuse object raw depth"),
    )
    box = o3d.geometry.TriangleMesh.create_box(0.1, 0.2, 0.1)
    box.compute_vertex_normals()

    def photo(*a, **kw):
        if not succeeds:
            raise RuntimeError("Native unavailable")
        return box, {
            "method": "apple_masked_photogrammetry",
            "geometry_completeness": {"low_quality": False, "warnings": []},
            "registration": {},
        }

    monkeypatch.setattr(pipeline, "reconstruct_images", photo)

    def texture(*a, **kw):
        assert kw["visibility"] == "mesh"
        return (
            np.asarray(box.vertices),
            np.asarray(box.vertex_normals),
            np.zeros((8, 2)),
            np.asarray(box.triangles),
            np.zeros((2048, 2048, 3), np.uint8),
        ), {"observed_texel_fraction": 0.8}

    monkeypatch.setattr(pipeline, "bake_texture", texture)
    pipeline.process_scan(
        tmp_path,
        tmp_path / "work",
        lambda *a: None,
        lambda: None,
        options=pipeline.ScanOptions(),
    )
    proposal = json.loads((tmp_path / "work/outputs/proposals.json").read_text())[
        "proposals"
    ][0]
    assert (proposal["mesh_url"] is not None) == succeeds
    if succeeds:
        assert proposal["method"] == "apple_masked_photogrammetry"
    else:
        assert "no reliable depth extent" in proposal["failure_reason"]


def test_export_closure_welds_uv_seams_but_rejects_disconnected_parts(tmp_path):
    from housing_segmentation.captures.glb import write_glb
    from housing_segmentation.captures.validation import validate_phone_glb

    texture = np.zeros((2048, 2048, 3), np.uint8)

    def export(mesh, path):
        # Duplicate each face's vertices exactly as independent UV charts do.
        positions = np.asarray(mesh.vertices)[np.asarray(mesh.triangles)].reshape(-1, 3)
        faces = np.arange(len(positions)).reshape(-1, 3)
        write_glb(
            path,
            positions,
            np.tile([0, 1, 0], (len(positions), 1)),
            np.zeros((len(positions), 2)),
            faces,
            texture,
        )

    cube = o3d.geometry.TriangleMesh.create_box(0.1, 0.1, 0.1)
    export(cube, tmp_path / "closed.glb")
    result = validate_phone_glb(tmp_path / "closed.glb", require_closed=True)
    assert result["closed_surface"]["components"] == 1
    disconnected = cube + o3d.geometry.TriangleMesh.create_box(0.1, 0.1, 0.1).translate(
        [1, 0, 0]
    )
    export(disconnected, tmp_path / "fragmented.glb")
    with pytest.raises(ValueError, match="one watertight"):
        validate_phone_glb(tmp_path / "fragmented.glb", require_closed=True)
