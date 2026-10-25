"""CPU ray-cast fixture, with no OpenGL context or synthetic masks used by production."""
import json
from pathlib import Path
import zipfile

import numpy as np
from PIL import Image

from .format import CONVENTIONS, SCHEMA, column_major


def look_at(position, target):
    back = np.asarray(position) - target
    back /= np.linalg.norm(back)
    right = np.cross([0., 1., 0.], back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    pose = np.eye(4)
    pose[:3, :3] = np.column_stack([right, up, back])
    pose[:3, 3] = position
    return pose


def generate_capture(folder, *, frames=36, width=320, height=240, capture_id="synthetic_box", tilt_anchor=False):
    import open3d as o3d
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=False)
    for name in ("frames", "depth", "confidence", "truth_masks"):
        (root / name).mkdir()
    scene = o3d.t.geometry.RaycastingScene(nthreads=1)
    box_min = np.array([-.20, .61, -.15])
    dimensions = np.array([.40, .30, .30])
    box = o3d.geometry.TriangleMesh.create_box(*dimensions).translate(box_min)
    table = o3d.geometry.TriangleMesh.create_box(2.4, .06, 1.8).translate([-1.2, .55, -.9])
    wall = o3d.geometry.TriangleMesh.create_box(3.4, 2.0, .06).translate([-1.7, 0, -1.45])
    floor = o3d.geometry.TriangleMesh.create_box(4, .05, 4).translate([-2, -.05, -2])
    cylinder = o3d.geometry.TriangleMesh.create_cylinder(.10, .25, resolution=40)
    cylinder.rotate(o3d.geometry.get_rotation_matrix_from_xyz((np.pi / 2, 0, 0)), center=[0, 0, 0]).translate([.57, .735, .14])
    left_wall = o3d.geometry.TriangleMesh.create_box(.06, 2., 2.9).translate([-1.65, 0, -1.45])
    right_wall = o3d.geometry.TriangleMesh.create_box(.06, 2., 2.9).translate([1.59, 0, -1.45])
    front_wall = o3d.geometry.TriangleMesh.create_box(3.4, 2., .06).translate([-1.7, 0, 1.39])
    for mesh in (box, table, wall, floor, cylinder, left_wall, right_wall, front_wall):
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    anchor_to_world = np.eye(4)
    if tilt_anchor:
        anchor_to_world[:3, :3] = o3d.geometry.get_rotation_matrix_from_xyz((.2, .3, -.15))
        anchor_to_world[:3, 3] = [.4, -.1, .2]
    world_to_anchor = np.linalg.inv(anchor_to_world)
    # Scene/world geometry is gravity-aligned; transform into the deliberately tilted anchor.
    def anchor_pose(world_pose):
        return world_to_anchor @ world_pose
    corners = np.array([[x, y, z] for x in (box_min[0], box_min[0] + dimensions[0]) for y in (box_min[1], box_min[1] + dimensions[1]) for z in (box_min[2], box_min[2] + dimensions[2])])
    anchor_corners = corners @ world_to_anchor[:3, :3].T + world_to_anchor[:3, 3]
    k = {"fx": width * 1.0, "fy": width * 1.0, "cx": width / 2, "cy": height / 2}
    dw, dh = width // 2 - 20, height // 2 - 12
    sx, sy, tx, ty = 2., 2., 20., 12.
    dk = {"fx": k["fx"] / sx, "fy": k["fy"] / sy, "cx": (k["cx"] - tx) / sx, "cy": (k["cy"] - ty) / sy}
    target = np.array([0., .75, 0.])
    records = []
    seed_mask = None
    for index in range(frames):
        angle = 2 * np.pi * index / frames
        # Camera stays above tabletop, observing box and surrounding support from all sides.
        position = target + [1.05 * np.sin(angle), .52 + .05 * np.sin(2 * angle), 1.05 * np.cos(angle)]
        pose = look_at(position, target)
        def cast(w, h, intrinsics):
            v, u = np.indices((h, w), dtype=np.float32)
            rays_cam = np.stack([(u + .5 - intrinsics["cx"]) / intrinsics["fx"],
                                 (intrinsics["cy"] - v - .5) / intrinsics["fy"], -np.ones_like(u)], -1)
            directions = rays_cam @ pose[:3, :3].T
            origins = np.broadcast_to(pose[:3, 3], directions.shape)
            hits = scene.cast_rays(o3d.core.Tensor(np.concatenate([origins, directions], -1).astype(np.float32)), nthreads=1)
            return hits["t_hit"].numpy(), hits["geometry_ids"].numpy(), origins, directions
        z, ids, origins, directions = cast(width, height, k)
        valid = np.isfinite(z)
        points = origins + np.where(valid, z, 0)[..., None] * directions
        checker = ((np.floor(points[..., 0] * 24) + np.floor(points[..., 1] * 24) + np.floor(points[..., 2] * 24)).astype(int) % 2)
        palette = np.array([[185, 75, 45], [135, 110, 70], [135, 170, 195], [110, 120, 125], [55, 155, 95]], dtype=float)
        color_ids = np.where(ids > 4, 2, ids)
        rgb = palette[np.minimum(color_ids, 4)] * (.65 + checker[..., None] * .35)
        rgb[~valid] = [205, 210, 215]
        # Deterministic small texture features support matching without modifying geometry.
        pattern = (np.sin(points[..., 0] * 170) * np.sin(points[..., 2] * 150) > .65)
        rgb[valid & pattern] = np.clip(rgb[valid & pattern] + 28, 0, 255)
        Image.fromarray(rgb.astype(np.uint8)).save(root / f"frames/{index:04d}.jpg", quality=96)
        mask = (ids == 0).astype(np.uint8) * 255
        Image.fromarray(mask).save(root / f"truth_masks/{index:04d}.png")
        if index == 0:
            seed_mask = mask
            Image.fromarray(mask).save(root / "seed_mask.png")
        dz, dids, _, _ = cast(dw, dh, dk)
        depth = np.where(np.isfinite(dz) & (dz > 0) & (dz < 10), dz * 1000, 0).round().astype("<u2")
        depth.tofile(root / f"depth/{index:04d}.bin")
        np.where(depth > 0, 255, 0).astype(np.uint8).tofile(root / f"confidence/{index:04d}.bin")
        records.append({"index": index, "timestamp_ns": 1_000_000_000 + index * 100_000_000, "tracking": "TRACKING",
                        "image": f"frames/{index:04d}.jpg", "width": width, "height": height, "intrinsics": k,
                        "camera_to_anchor": column_major(anchor_pose(pose)), "camera_to_world": column_major(pose),
                        "depth": {"file": f"depth/{index:04d}.bin", "width": dw, "height": dh,
                                  "timestamp_ns": 1_000_000_000 + index * 100_000_000, "intrinsics": dk, "to_image": [sx, sy, tx, ty]},
                        "confidence": {"file": f"confidence/{index:04d}.bin"}, "sharpness": 120., "orbit_deg": index * 360 / frames})
    yy, xx = np.nonzero(seed_mask)
    if not len(xx):
        raise RuntimeError("Synthetic seed object is not visible")
    centre_index = np.argmin((xx - xx.mean()) ** 2 + (yy - yy.mean()) ** 2)
    manifest = {"schema": SCHEMA, "capture_id": capture_id, "session_id": "synthetic_session", "mode": "object",
                "label": "Synthetic textured box", "units": "meters", "conventions": CONVENTIONS,
                "anchor_to_world": column_major(anchor_to_world),
                "seed": {"frame_index": 0, "points": [[int(xx[centre_index]), int(yy[centre_index]), 1]], "mask": "seed_mask.png",
                         "bbox_anchor": {"min": (anchor_corners.min(0) - .002).tolist(), "max": (anchor_corners.max(0) + .002).tolist()}},
                "frames": records}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    truth = {"object_dimensions_m": dimensions.tolist(), "object_min_world": box_min.tolist(),
             "object_max_world": (box_min + dimensions).tolist(), "table_top_world_y": .61,
             "description": "Textured box target, cylinder occluder, textured table, floor and surrounding walls; analytical metric geometry CPU raycast"}
    (root / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    return manifest


def zip_capture(root, output):
    root = Path(root)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("frames", "depth", "confidence"):
            for path in sorted((root / folder).iterdir()):
                archive.write(path, path.relative_to(root).as_posix())
        archive.write(root / "seed_mask.png", "seed_mask.png")
