"""Image-led object geometry, metric camera registration and bounded completion."""

import json
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from .format import Capture, matrix
from .geometry import gravity_frame, project


def depth_tap_position(capture, masks):
    """Find reliable masked depth at a capture seed; never average across edges."""
    from .geometry import anchor_depth_points, depth_to_image

    seed = getattr(capture.manifest, "seed", None)
    if seed is None or not hasattr(seed, "points"):
        return None
    position = next(
        i for i, f in enumerate(capture.manifest.frames) if f.index == seed.frame_index
    )
    frame = capture.manifest.frames[position]
    depth = capture.depth(frame)
    x, y = depth_to_image(frame)
    valid = (depth > 0) & (capture.confidence(frame) >= 192) & masks[position][y, x]
    positives = [(px, py) for px, py, label in seed.points if label == 1]
    if not positives or not valid.any():
        return None
    distance = np.min(
        [(x - px) ** 2 + (y - py) ** 2 for px, py in positives], axis=0
    ).astype(float)
    distance[~valid] = np.inf
    index = np.unravel_index(np.argmin(distance), distance.shape)
    if distance[index] > (3 * max(frame.depth.to_image[:2])) ** 2:
        return None
    return anchor_depth_points(frame, depth)[index]


def phone_images(capture):
    return (
        min(f.width for f in capture.manifest.frames) >= 1280
        and np.median([f.width / f.depth.width for f in capture.manifest.frames]) >= 6
    )


def mesh_stats(mesh):
    labels, counts, areas = mesh.cluster_connected_triangles()
    return {
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.triangles),
        "components": len(counts),
        "watertight": mesh.is_watertight(),
        "self_intersecting": mesh.is_self_intersecting(),
        "boundary_or_nonmanifold_edges": len(
            mesh.get_non_manifold_edges(allow_boundary_edges=False)
        ),
    }


def topology_repair(mesh, check):
    import open3d as o3d
    import pymeshfix

    mesh.remove_duplicated_vertices().remove_duplicated_triangles().remove_degenerate_triangles().remove_unreferenced_vertices()
    before = mesh_stats(mesh)
    lo, hi = np.asarray(mesh.vertices).min(0), np.asarray(mesh.vertices).max(0)
    labels, counts, areas = mesh.cluster_connected_triangles()
    if len(counts) > 1:
        mesh.remove_triangles_by_mask(np.asarray(labels) != int(np.argmax(areas)))
        mesh.remove_unreferenced_vertices()
    check()
    fix = pymeshfix.MeshFix(np.asarray(mesh.vertices), np.asarray(mesh.triangles))
    fix.repair(joincomp=False, remove_smallest_components=False)
    repaired = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(fix.points), o3d.utility.Vector3iVector(fix.faces)
    )
    repaired.compute_vertex_normals()
    p = np.asarray(repaired.vertices)
    lost = np.maximum((hi - lo) - np.ptp(p, axis=0), 0.0)
    report = {
        "input": before,
        "output": mesh_stats(repaired),
        "axis_shortfall_m": lost.tolist(),
        "method": "weld and MeshFix boundary/intersection repair",
    }
    if np.any(lost > np.maximum(0.015, (hi - lo) * 0.03)):
        raise ValueError(
            "Photogrammetry repair would erase object parts: " + json.dumps(report)
        )
    if (
        not repaired.is_watertight()
        or len(repaired.cluster_connected_triangles()[1]) != 1
    ):
        raise ValueError("Photogrammetry repair did not yield one watertight surface")
    return repaired, report


def simplify_solid(mesh, maximum=50000):
    """Manifold simplification keeps closed topology instead of deleting islands."""
    import manifold3d as manifold
    import open3d as o3d

    mesh.remove_duplicated_vertices().remove_degenerate_triangles().remove_unreferenced_vertices()
    solid = manifold.Manifold(
        manifold.Mesh(
            np.ascontiguousarray(mesh.vertices, dtype=np.float32),
            np.ascontiguousarray(mesh.triangles, dtype=np.uint32),
        )
    )
    if solid.status() != manifold.Error.NoError:
        raise ValueError(
            "Surface completion is not a valid closed manifold: " + str(solid.status())
        )
    if len(mesh.triangles) <= maximum and mesh.is_watertight():
        return mesh
    for tolerance in (0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003, 0.004, 0.006, 0.008):
        candidate = solid.simplify(tolerance)
        if candidate.num_tri() > maximum:
            continue
        data = candidate.to_mesh()
        result = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(np.asarray(data.vert_properties)[:, :3].copy()),
            o3d.utility.Vector3iVector(np.array(data.tri_verts, copy=True)),
        )
        result.compute_vertex_normals()
        if result.is_watertight() and not result.is_self_intersecting():
            return result
    raise ValueError("Could not retain a watertight object within the triangle budget")


def photo_volume_repair(
    mesh,
    capture,
    masks,
    check,
    *,
    maximum=50000,
    pitch=0.003,
    point_anchor=None,
    source="photogrammetry",
):
    """Close a photo surface at RGB precision, retaining mask-supported foot parts.

    This voxelizes triangles reconstructed from RGB, not phone depth. Small
    detached parts may join only over a short, silhouette-supported bridge.
    """
    import open3d as o3d
    import trimesh
    from scipy import ndimage
    from scipy.spatial import cKDTree
    from skimage.measure import marching_cubes

    started = perf_counter()
    check()
    span = np.ptp(mesh.vertices, axis=0)
    # Bound the dense closure grid on large furniture. This changes only the
    # completion precision; RGB photogrammetry itself retains full resolution.
    pitch = max(pitch, float((np.prod(span + pitch * 6) / 20_000_000) ** (1 / 3)))
    voxel = trimesh.Trimesh(
        np.asarray(mesh.vertices), np.asarray(mesh.triangles), process=False
    ).voxelized(pitch)
    origin = voxel.transform[:3, 3] - pitch * 3
    occupied = np.pad(voxel.matrix, 3)
    occupied = ndimage.binary_fill_holes(ndimage.binary_closing(occupied, iterations=2))
    labels, count = ndimage.label(occupied)
    if not count:
        raise ValueError("Surface closure has no supported occupied component")
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    chosen = int(sizes.argmax())
    if point_anchor is not None:
        q = np.rint((np.asarray(point_anchor) - origin) / pitch).astype(int)
        if np.all(q >= 0) and np.all(q < labels.shape) and labels[tuple(q)]:
            chosen = int(labels[tuple(q)])
    main = labels == chosen
    bridges = []
    rejected = []
    bridge_limit = (
        min(0.18, float(span.max()) * 0.25) if source == "photogrammetry" else 0.0
    )
    for part in np.argsort(sizes)[::-1]:
        if not part or part == chosen or sizes[part] < 8:
            continue
        check()
        a = np.argwhere(main)
        b = np.argwhere(labels == part)
        distances, nearest = cKDTree(a).query(b)
        i = int(np.argmin(distances))
        gap = float(distances[i] * pitch)
        start = origin + a[nearest[i]] * pitch
        end = origin + b[i] * pitch
        line = np.linspace(start, end, max(12, int(gap / pitch) + 1))
        support = np.zeros(len(line))
        seen = np.zeros(len(line))
        if gap <= bridge_limit:
            for frame, mask in zip(capture.manifest.frames, masks):
                pixels, z = project(line, frame)
                xy = np.floor(pixels).astype(int)
                good = (
                    (z > 0.01)
                    & (xy[:, 0] >= 0)
                    & (xy[:, 0] < frame.width)
                    & (xy[:, 1] >= 0)
                    & (xy[:, 1] < frame.height)
                )
                distance = cv2.distanceTransform(
                    (~mask).astype(np.uint8), cv2.DIST_L2, 3
                )
                ids = np.flatnonzero(good)
                seen[ids] += 1
                support[ids] += (
                    distance[xy[ids, 1], xy[ids, 0]]
                    <= frame.intrinsics.fx * 0.008 / z[ids]
                )
        fraction = float(np.min(support / np.maximum(seen, 1)))
        if gap > bridge_limit or np.min(seen) < 6 or fraction < 0.8:
            rejected.append(
                {
                    "voxels": int(sizes[part]),
                    "gap_m": gap,
                    "minimum_mask_agreement": fraction,
                }
            )
            continue
        # A narrow capsule closes only the measured missing leg connection.
        indices = np.rint((line - origin) / pitch).astype(int)
        tube = np.zeros_like(main)
        tube[tuple(indices.T)] = True
        main |= (labels == part) | ndimage.binary_dilation(tube, iterations=2)
        bridges.append(
            {"gap_m": gap, "radius_m": pitch * 2, "minimum_mask_agreement": fraction}
        )
    main = ndimage.binary_fill_holes(main)
    field = ndimage.gaussian_filter(main.astype(np.float32), 0.6)
    vertices, faces, _, _ = marching_cubes(
        field, 0.50317, spacing=(pitch,) * 3, allow_degenerate=False
    )
    closed = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(vertices + origin), o3d.utility.Vector3iVector(faces)
    )
    # Simplification is followed by intersection repair, with an extent guard.
    import manifold3d as manifold

    solid = manifold.Manifold(
        manifold.Mesh(
            np.ascontiguousarray(closed.vertices, dtype=np.float32),
            np.ascontiguousarray(closed.triangles, dtype=np.uint32),
        )
    )
    if solid.status() != manifold.Error.NoError:
        raise ValueError("Photo surface cannot be closed safely")
    errors = []
    for tolerance in (0.001, 0.0015, 0.002, 0.003, 0.004):
        candidate = solid.simplify(tolerance)
        if candidate.num_tri() > maximum:
            continue
        data = candidate.to_mesh()
        candidate = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(
                np.array(data.vert_properties[:, :3], copy=True)
            ),
            o3d.utility.Vector3iVector(np.array(data.tri_verts, copy=True)),
        )
        try:
            result, repair = topology_repair(candidate, check)
            # Also compare with the ORIGINAL photo, including detached parts.
            lost = np.maximum(
                np.ptp(mesh.vertices, axis=0) - np.ptp(result.vertices, axis=0), 0.0
            )
            if np.any(lost > np.maximum(0.015, np.ptp(mesh.vertices, axis=0) * 0.03)):
                raise ValueError("Photo completion lost original extent")
            return result, {
                "method": f"{source} surface closure",
                "voxel_m": pitch,
                "input_components": count,
                "mask_supported_bridges": bridges,
                "rejected_components": rejected,
                "repair": repair,
                "seconds": perf_counter() - started,
                "raw_depth_used_for_surface": source == "depth",
                "output": mesh_stats(result),
            }
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError("No complete watertight photo surface: " + "; ".join(errors))


def reconstruct_images(
    capture, masks_dir, output, check, *, timeout=900, maximum=50000, point_anchor=None
):
    """Genuine masked photogrammetry first; no TSDF size veto."""
    import open3d as o3d

    from ..scans.tapped import MaskFiles
    from .object_capture import run_object_capture

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    mesh, registration = run_object_capture(
        capture, masks_dir, output, check, timeout=timeout
    )
    o3d.io.write_triangle_mesh(str(output / "registered.ply"), mesh)
    reference = np.asarray(mesh.vertices).copy()
    stages = {"photogrammetry": mesh_stats(mesh)}
    try:
        mesh, repair = topology_repair(mesh, check)
        mesh = simplify_solid(mesh, maximum)
        stages["repair"] = repair
    except ValueError as exc:
        stages["boundary_repair_rejected"] = str(exc)
        # Reload because boundary repair may have changed the source in place.
        mesh = o3d.io.read_triangle_mesh(str(output / "registered.ply"))
        mesh, repair = photo_volume_repair(
            mesh,
            capture,
            MaskFiles(masks_dir, capture.manifest.frames),
            check,
            maximum=maximum,
            point_anchor=point_anchor,
        )
        stages["surface_completion"] = repair
    stages["final"] = mesh_stats(mesh)
    from .quality import height_evidence

    up = gravity_frame(capture.manifest.anchor_to_world)[:, 1]
    stages["height_evidence"] = {
        "reference": "gravity plane through anchor origin, metres",
        "photogrammetry": height_evidence(reference, up),
        "final": height_evidence(mesh.vertices, up),
    }
    span = np.ptp(reference, axis=0)
    shortfall = np.maximum(span - np.ptp(mesh.vertices, axis=0), 0.0)
    incomplete = bool(np.any(shortfall > np.maximum(0.015, span * 0.03)))
    if incomplete:
        raise ValueError(
            "Image reconstruction erased a substantial part of the photo surface"
        )
    report = {
        "method": "apple_masked_photogrammetry",
        "registration": registration,
        "stages": stages,
        "metric_scale_source": "robust similarity fit of recovered camera centres to metric ARCore VIO centres",
        "camera_input": "Apple estimates cameras internally; VIO aligns metric output (not a pose-locked solver)",
        "raw_depth_used_for_surface": False,
        "seconds": perf_counter() - started,
        "geometry_completeness": {
            "low_quality": False,
            "warnings": [],
            "reference": "unrepaired RGB photogrammetry extent",
            "axis_shortfall_m": shortfall.tolist(),
        },
    }
    outline = silhouette_quality(
        texture_cameras(capture, report),
        MaskFiles(masks_dir, capture.manifest.frames),
        mesh,
        check,
    )
    report["silhouette_validation"] = outline
    report["geometry_completeness"].update(
        low_quality=outline["low_quality"], warnings=outline["warnings"]
    )
    report["seconds"] = perf_counter() - started
    o3d.io.write_triangle_mesh(str(output / "selected.ply"), mesh)
    (output / "geometry.json").write_text(json.dumps(report, indent=2) + "\n")
    return mesh, report


def texture_cameras(capture, report):
    """Use recovered photo cameras for source sampling, with VIO as fallback."""
    from types import SimpleNamespace

    from .format import Intrinsics

    cameras = report.get("registration", {}).get("texture_cameras", {})
    frames = []
    for frame in capture.manifest.frames:
        updates = dict(cameras.get(str(frame.index), {}))
        if "intrinsics" in updates:
            updates["intrinsics"] = Intrinsics(**updates["intrinsics"])
        frames.append(frame.model_copy(update=updates))
    return Capture(
        capture.root,
        SimpleNamespace(
            frames=frames, anchor_to_world=capture.manifest.anchor_to_world
        ),
    )


def adaptive_depth(
    capture,
    masks,
    check,
    *,
    extent,
    point_anchor=None,
    maximum=50000,
    silhouette_masks=None,
):
    """Explicit low-resolution fallback; one closed tap-connected surface only."""
    from .fusion import fuse_depth

    if point_anchor is None:
        point_anchor = depth_tap_position(capture, masks)
    samples = []
    spatial = []
    for frame, mask in zip(capture.manifest.frames, masks):
        from .geometry import depth_to_image

        x, y = depth_to_image(frame)
        depth = capture.depth(frame)
        values = (
            depth[(depth > 0) & (capture.confidence(frame) >= 192) & mask[y, x]] / 1000
        )
        if len(values):
            samples.extend(values[:: max(1, len(values) // 100)])
            spatial.append(
                float(
                    np.median(values)
                    / min(frame.depth.intrinsics.fx, frame.depth.intrinsics.fy)
                )
            )
    if not samples:
        raise ValueError(
            "No high-confidence object depth for the explicit depth fallback"
        )
    spreads = [
        (p["residual_p01_p99_m"][1] - p["residual_p01_p99_m"][0]) / 4.65
        for p in extent.planes
        if "residual_p01_p99_m" in p
    ]
    noise = max(0.02, float(np.median(spreads)) if spreads else 0.02)
    voxel = max(0.02, noise, float(np.median(spatial)) * 1.5)
    mesh, report, _ = fuse_depth(
        capture,
        masks,
        check,
        object_only=True,
        voxel=voxel,
        extent=extent,
        silhouette_masks=silhouette_masks,
        minimum_confidence=192,
    )
    labels, counts, areas = mesh.cluster_connected_triangles()
    labels = np.asarray(labels)
    vertices = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.triangles)
    if point_anchor is None:
        chosen = int(np.argmax(areas))
        selection = "largest component (no metric tap supplied)"
    else:
        centres = vertices[faces].mean(1)
        distances = np.linalg.norm(centres - np.asarray(point_anchor), axis=1)
        chosen = int(labels[np.argmin(distances)])
        selection = "nearest surface component to metric tap"
    mesh.remove_triangles_by_mask(labels != chosen)
    mesh.remove_unreferenced_vertices()
    mesh, repair = photo_volume_repair(
        mesh, capture, masks, check, maximum=maximum, pitch=voxel / 2, source="depth"
    )
    report.update(
        method="adaptive_depth_fallback",
        noise_sigma_m=noise,
        depth_pixel_footprint_m=float(np.median(spatial)),
        component_selection=selection,
        components_before_selection=len(counts),
        closure=repair,
        output=mesh_stats(mesh),
    )
    return mesh, report


def silhouette_quality(capture, masks, mesh, check):
    """Check missing upper/lower geometry against all independent RGB outlines."""
    import open3d as o3d

    scene = o3d.t.geometry.RaycastingScene(nthreads=2)
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    centre = np.asarray(mesh.vertices).mean(0)
    up = gravity_frame(capture.manifest.anchor_to_world)[:, 1]
    rows = []
    for frame, mask in zip(capture.manifest.frames, masks):
        check()
        scale = min(1.0, 480 / frame.width)
        w, h = round(frame.width * scale), round(frame.height * scale)
        yy, xx = np.indices((h, w))
        k = frame.intrinsics
        pose = matrix(frame.camera_to_anchor)
        direction = (
            np.stack(
                [
                    ((xx + 0.5) / scale - k.cx) / k.fx,
                    (k.cy - (yy + 0.5) / scale) / k.fy,
                    -np.ones((h, w)),
                ],
                axis=-1,
            )
            @ pose[:3, :3].T
        )
        rays = np.concatenate(
            [np.broadcast_to(pose[:3, 3], direction.shape), direction], axis=-1
        ).astype(np.float32)
        hits = scene.cast_rays(o3d.core.Tensor(rays), nthreads=2)["t_hit"].numpy()
        predicted = np.isfinite(hits)
        truth = (
            cv2.resize(mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
            > 0
        )
        if truth.sum() < 10:
            continue
        intersection = predicted & truth
        p, _ = project(np.array([centre, centre + up * 0.2]), frame)
        vertical = p[1] - p[0]
        vertical /= max(np.linalg.norm(vertical), 1e-8)
        height = xx * vertical[0] + yy * vertical[1]
        low, high = np.quantile(height[truth], [0.01, 0.99])
        bottom = truth & (height <= low + 0.25 * (high - low))
        top = truth & (height >= high - 0.25 * (high - low))
        rows.append(
            {
                "frame_index": frame.index,
                "recall": float(intersection.sum() / truth.sum()),
                "precision": float(intersection.sum() / max(1, predicted.sum())),
                "lower_recall": float(
                    (predicted & bottom).sum() / max(1, bottom.sum())
                ),
                "upper_recall": float((predicted & top).sum() / max(1, top.sum())),
            }
        )
    if not rows:
        raise ValueError("No usable RGB outlines for geometry validation")
    medians = {
        key: float(np.median([r[key] for r in rows]))
        for key in ("recall", "precision", "lower_recall", "upper_recall")
    }
    warnings = []
    for key, minimum in [
        ("recall", 0.8),
        ("precision", 0.75),
        ("lower_recall", 0.65),
        ("upper_recall", 0.75),
    ]:
        if medians[key] < minimum:
            warnings.append(
                f"RGB silhouette {key} {medians[key]:.1%} is below {minimum:.0%}; object geometry is incomplete or spills outside its outline"
            )
    return {
        "low_quality": bool(warnings),
        "warnings": warnings,
        "median": medians,
        "views": rows,
        "render_width": 480,
        "source": "all SAM RGB outlines, mesh visibility; no raw depth or manual size",
    }
