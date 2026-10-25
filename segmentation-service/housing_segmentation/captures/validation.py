from io import BytesIO

import numpy as np
from PIL import Image

from .format import matrix
from .geometry import transform_points
from .glb import read_accessor, read_glb


def validate_phone_glb(path, *, require_closed=False):
    document, binary = read_glb(path)
    if document.get("asset", {}).get("version") != "2.0" or len(document.get("nodes", [])) != 1 or len(document.get("meshes", [])) != 1:
        raise ValueError("GLB must have one node/mesh and glTF 2.0")
    if document["nodes"] != [{"mesh": 0}] or document.get("extensionsRequired") or document.get("extensionsUsed"):
        raise ValueError("GLB has a transform/hierarchy/compression extension")
    primitives = document["meshes"][0]["primitives"]
    if len(primitives) != 1 or primitives[0].get("mode") != 4 or set(primitives[0]["attributes"]) != {"POSITION", "NORMAL", "TEXCOORD_0"}:
        raise ValueError("GLB primitive/attributes violate the phone contract")
    for name, kind in (("POSITION", "VEC3"), ("NORMAL", "VEC3"), ("TEXCOORD_0", "VEC2")):
        accessor = document["accessors"][primitives[0]["attributes"][name]]
        if accessor.get("componentType") != 5126 or accessor.get("type") != kind or accessor.get("normalized", False):
            raise ValueError("GLB attributes must be unnormalized float32 vectors")
    if len(document["images"]) != 1 or len(document["textures"]) != 1 or len(document["materials"]) != 1:
        raise ValueError("GLB must use one embedded texture/material")
    primitive = primitives[0]
    index_accessor = document["accessors"][primitive["indices"]]
    if index_accessor["componentType"] != 5125 or index_accessor["count"] % 3 or index_accessor["count"] // 3 > 50000:
        raise ValueError("GLB indices must be uint32 triangles, at most 50k")
    arrays = [read_accessor(document, binary, primitive["attributes"][key]) for key in ("POSITION", "NORMAL", "TEXCOORD_0")]
    if any(not np.isfinite(array).all() for array in arrays) or len({len(array) for array in arrays}) != 1:
        raise ValueError("GLB attributes are invalid")
    indices = read_accessor(document, binary, primitive["indices"])
    if indices.size == 0 or indices.max() >= len(arrays[0]):
        raise ValueError("GLB index outside vertex buffer")
    image = document["images"][0]
    if "uri" in image or image.get("mimeType") != "image/png":
        raise ValueError("Texture must be an embedded PNG")
    view = document["bufferViews"][image["bufferView"]]
    with Image.open(BytesIO(binary[view["byteOffset"]:view["byteOffset"] + view["byteLength"]])) as texture:
        if texture.size != (2048, 2048):
            raise ValueError("Texture must be 2048 square")
    result = {"triangles": index_accessor["count"] // 3, "vertices": len(arrays[0]), "texture_size": [2048, 2048], "contract_valid": True}
    if require_closed:
        import open3d as o3d
        from .image_geometry import mesh_stats
        mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(arrays[0].astype(float)),
                                       o3d.utility.Vector3iVector(indices.reshape(-1, 3).astype(int)))
        # UV charts duplicate vertices; connectivity must be checked after welding.
        mesh.remove_duplicated_vertices().remove_degenerate_triangles().remove_unreferenced_vertices()
        result["closed_surface"] = mesh_stats(mesh)
        if not mesh.is_watertight() or len(mesh.cluster_connected_triangles()[1]) != 1 or mesh.is_self_intersecting():
            raise ValueError("Exported object must have one watertight, non-intersecting surface after welding UV seams")
    return result


def synthetic_novel_view_coverage(capture, background_glb, truth):
    """Ray-test the 3D patch behind the ground-truth object from shifted viewpoints."""
    import open3d as o3d
    document, binary = read_glb(background_glb)
    vertices = read_accessor(document, binary, 0)
    indices = read_accessor(document, binary, 3).reshape(-1, 3)
    background = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices.astype(float)), o3d.utility.Vector3iVector(indices.astype(int)))
    scene = o3d.t.geometry.RaycastingScene(nthreads=1)
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(background))
    dimensions = np.asarray(truth["object_dimensions_m"])
    target = o3d.geometry.TriangleMesh.create_box(*dimensions).translate(truth["object_min_world"])
    target.transform(np.linalg.inv(matrix(capture.manifest.anchor_to_world)))
    object_scene = o3d.t.geometry.RaycastingScene(nthreads=1)
    object_scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(target))
    reports = []
    for index in (0, len(capture.manifest.frames) // 4, len(capture.manifest.frames) // 2):
        frame = capture.manifest.frames[index]
        pose = matrix(frame.camera_to_anchor)
        pose[:3, 3] += [.03, .01, .02]
        # This shifted camera is not one of the recorded camera positions.
        v, u = np.indices((frame.height, frame.width), dtype=np.float32)
        k = frame.intrinsics
        direction = np.stack([(u + .5 - k.cx) / k.fx, (k.cy - v - .5) / k.fy, -np.ones_like(u)], -1) @ pose[:3, :3].T
        rays = np.concatenate([np.broadcast_to(pose[:3, 3], direction.shape), direction], -1).astype(np.float32)
        object_hits = object_scene.cast_rays(o3d.core.Tensor(rays), nthreads=1)["t_hit"].numpy()
        background_hits = scene.cast_rays(o3d.core.Tensor(rays), nthreads=1)["t_hit"].numpy()
        visible = np.isfinite(object_hits)
        covered = visible & np.isfinite(background_hits) & (background_hits > object_hits - .005)
        reports.append({"source_frame_index": frame.index, "camera_shift_anchor_m": [.03, .01, .02],
                        "target_pixels": int(visible.sum()), "covered_by_3d_patch": int(covered.sum()),
                        "coverage_fraction": float(covered.sum() / max(1, visible.sum()))})
    return reports


def estimate_patch_coverage(capture, object_glb, background_glb, object_to_anchor):
    """Conservative quality gate using the reconstructed target and shifted scan cameras."""
    import open3d as o3d
    scenes = []
    for path, transform in ((object_glb, matrix(object_to_anchor)), (background_glb, np.eye(4))):
        document, binary = read_glb(path)
        vertices = transform_points(read_accessor(document, binary, 0), transform)
        faces = read_accessor(document, binary, 3).reshape(-1, 3)
        mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices), o3d.utility.Vector3iVector(faces.astype(int)))
        scene = o3d.t.geometry.RaycastingScene(nthreads=1)
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
        scenes.append(scene)
    results = []
    for index in np.unique(np.linspace(0, len(capture.manifest.frames) - 1, min(8, len(capture.manifest.frames))).astype(int)):
        frame = capture.manifest.frames[index]
        pose = matrix(frame.camera_to_anchor)
        pose[:3, 3] += [.03, .01, .02]
        scale = min(1., 240 / frame.width)
        width, height = round(frame.width * scale), round(frame.height * scale)
        y, x = np.indices((height, width), dtype=np.float32)
        k = frame.intrinsics
        direction = np.stack([(x + .5 - k.cx * scale) / (k.fx * scale), (k.cy * scale - y - .5) / (k.fy * scale), -np.ones_like(x)], -1) @ pose[:3, :3].T
        rays = o3d.core.Tensor(np.concatenate([np.broadcast_to(pose[:3, 3], direction.shape), direction], -1).astype(np.float32))
        target = scenes[0].cast_rays(rays, nthreads=1)["t_hit"].numpy()
        background = scenes[1].cast_rays(rays, nthreads=1)["t_hit"].numpy()
        selected = np.isfinite(target)
        covered = selected & np.isfinite(background) & (background >= target - .005)
        results.append({"frame_index": frame.index, "target_pixels": int(selected.sum()),
                        "fraction": float(covered.sum() / max(1, selected.sum()))})
    return {"views": results, "minimum_fraction": min(result["fraction"] for result in results),
            "camera_shift_anchor_m": [.03, .01, .02], "target": "reconstructed object mesh; not ground truth"}
