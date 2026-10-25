import gc
import hashlib
import os
from pathlib import Path

import cv2
import numpy as np

from ..config import SERVICE_ROOT
from .format import matrix
from .geometry import project

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

LAMA_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
LAMA_SOURCE = "https://github.com/advimman/lama"
LAMA_MD5 = "e3aa4aaa15225a33ec84f9f4bc47e500"


def inpaint_texture(rgb, missing, *, backend="lama", device="auto", model_path=None):
    report = {"requested_backend": backend, "backend": backend, "device": "cpu", "warnings": []}
    if not missing.any():
        report["backend"] = "none_needed"
        return rgb, report
    if backend == "lama":
        weights = Path(model_path or os.getenv("LAMA_MODEL_PATH", SERVICE_ROOT / ".cache/inpainting/big-lama.pt"))
        try:
            import torch
            if not weights.is_file():
                raise RuntimeError("LaMa checkpoint is not installed")
            data = weights.read_bytes()
            if hashlib.md5(data).hexdigest() != LAMA_MD5:
                raise RuntimeError("LaMa checkpoint integrity mismatch")
            report.update(source=LAMA_SOURCE, source_license="Apache-2.0", checkpoint_url=LAMA_URL,
                          checkpoint_sha256=hashlib.sha256(data).hexdigest())
            del data
            actual = ("mps" if torch.backends.mps.is_available() else "cpu") if device == "auto" else device
            model = torch.jit.load(str(weights), map_location="cpu").eval().to(actual)
            # Fourier inpainting at 512px, then composite only unobserved atlas texels.
            resized = cv2.resize(rgb, (512, 512), interpolation=cv2.INTER_AREA)
            resized_mask = cv2.resize(missing.astype(np.uint8), (512, 512), interpolation=cv2.INTER_NEAREST)
            image_tensor = torch.from_numpy(resized.transpose(2, 0, 1).copy()).float()[None].to(actual) / 255
            mask_tensor = torch.from_numpy(resized_mask.copy()).float()[None, None].to(actual)
            with torch.inference_mode():
                prediction = model(image_tensor, mask_tensor)
                filled = (prediction[0].permute(1, 2, 0).detach().cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
            filled = cv2.resize(filled, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
            result = rgb.copy()
            result[missing] = filled[missing]
            report["device"] = actual
            del model, prediction, image_tensor, mask_tensor
            gc.collect()
            if actual == "mps":
                torch.mps.empty_cache()
            return result, report
        except Exception as exc:
            report["warnings"].append(f"LaMa unavailable/failed; explicit OpenCV texture-space fallback: {exc}")
            report["backend"] = "opencv_telea"
    elif backend == "opencv":
        report["backend"] = "opencv_telea"
    else:
        raise ValueError("Inpainting backend must be lama or opencv")
    filled = cv2.inpaint(rgb, missing.astype(np.uint8) * 255, 5, cv2.INPAINT_TELEA)
    report["device"] = "cpu"
    return filled, report


def parameterize(mesh):
    import xatlas
    vertices = np.asarray(mesh.vertices).astype(np.float32)
    normals = np.asarray(mesh.vertex_normals).astype(np.float32)
    mapping, faces, uv = xatlas.parametrize(vertices, np.asarray(mesh.triangles).astype(np.uint32))
    return vertices[mapping], normals[mapping], np.asarray(uv), np.asarray(faces)


def rasterize_uv(vertices, normals, uv, faces, resolution=2048):
    positions = np.full((resolution, resolution, 3), np.nan, np.float32)
    directions = np.zeros_like(positions)
    for triangle in faces:
        xy = uv[triangle] * (resolution - 1)
        lo = np.floor(xy.min(0)).astype(int).clip(0, resolution - 1)
        hi = np.ceil(xy.max(0)).astype(int).clip(0, resolution - 1)
        xx, yy = np.meshgrid(np.arange(lo[0], hi[0] + 1), np.arange(lo[1], hi[1] + 1))
        a, b, c = xy
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-8:
            continue
        w0 = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / denominator
        w1 = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / denominator
        weights = np.stack([w0, w1, 1 - w0 - w1], -1)
        inside = np.all(weights >= -1e-5, axis=-1)
        x, y = xx[inside], yy[inside]
        positions[y, x] = weights[inside] @ vertices[triangle]
        directions[y, x] = weights[inside] @ normals[triangle]
    used = np.isfinite(positions[..., 0])
    directions[used] /= np.maximum(np.linalg.norm(directions[used], axis=1, keepdims=True), 1e-8)
    return positions, directions, used


def bake_texture(capture, masks, mesh, check, *, object_only, inpainting="lama", device="auto", visibility="depth"):
    if visibility not in ("depth", "mesh"):
        raise ValueError("Texture visibility must be depth or mesh")
    scene = None
    if visibility == "mesh":
        import open3d as o3d
        scene = o3d.t.geometry.RaycastingScene(nthreads=2)
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    vertices, normals, uv, faces = parameterize(mesh)
    positions, directions, used = rasterize_uv(vertices, normals, uv, faces)
    yy, xx = np.nonzero(used)
    world = positions[used].astype(np.float64)
    surface_normals = directions[used].astype(np.float64)
    best = np.zeros(len(world), np.float32)
    colors = np.zeros((len(world), 3), np.float32)
    sums = np.zeros_like(colors)
    weight_sum = np.zeros(len(world), np.float32)
    source_ids = np.full(len(world), -1, np.int16)
    for i, (frame, mask) in enumerate(zip(capture.manifest.frames, masks)):
        check()
        rgb, depth, confidence = capture.image(frame), capture.depth(frame), capture.confidence(frame)
        pixels, z = project(world, frame)
        x = np.floor(pixels[:, 0]).astype(int)
        y = np.floor(pixels[:, 1]).astype(int)
        sx, sy, tx, ty = frame.depth.to_image
        dx = np.floor((pixels[:, 0] - tx) / sx).astype(int)
        dy = np.floor((pixels[:, 1] - ty) / sy).astype(int)
        good = (z > .01) & (x >= 0) & (x < frame.width) & (y >= 0) & (y < frame.height) & (dx >= 0) & (dx < frame.depth.width) & (dy >= 0) & (dy < frame.depth.height)
        ids = np.nonzero(good)[0]
        if not len(ids):
            continue
        xg, yg, dxg, dyg = x[ids], y[ids], dx[ids], dy[ids]
        source_depth = depth[dyg, dxg] / 1000
        if scene is None:
            visible = (source_depth > 0) & (confidence[dyg, dxg] >= 64) & (np.abs(source_depth - z[ids]) < .025)
        else:
            # Ray distance to the reconstructed surface replaces the unreliable
            # 160x90 depth gate. A ray ends at the atlas texel, so t=1 there.
            camera = matrix(frame.camera_to_anchor)[:3, 3]
            rays = np.concatenate([np.broadcast_to(camera, (len(ids), 3)), world[ids] - camera], axis=1).astype(np.float32)
            hits = scene.cast_rays(o3d.core.Tensor(rays), nthreads=2)["t_hit"].numpy()
            visible = np.isfinite(hits) & (np.abs(hits - 1) * np.linalg.norm(rays[:, 3:], axis=1) < .008)
        if object_only:
            visible &= mask[yg, xg]
        else:
            padded_mask = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
            visible &= ~padded_mask[yg, xg]
        ids = ids[visible]
        if not len(ids):
            continue
        camera = matrix(frame.camera_to_anchor)[:3, 3]
        view = camera - world[ids]
        view /= np.maximum(np.linalg.norm(view, axis=1, keepdims=True), 1e-8)
        angle = np.abs(np.sum(surface_normals[ids] * view, axis=1))
        weights = (angle ** 2 / np.maximum(z[ids] ** 2, .01)).astype(np.float32)
        sampled = rgb[y[ids], x[ids]].astype(np.float32)
        sums[ids] += sampled * weights[:, None]
        weight_sum[ids] += weights
        better = weights > best[ids]
        selected = ids[better]
        best[selected] = weights[better]
        colors[selected] = sampled[better]
        source_ids[selected] = i
    observed = weight_sum > 0
    if observed.sum() < max(10, len(world) * .005):
        raise ValueError("Too few genuinely visible non-occluded texels to texture the 3D mesh")
    colors[observed] = .85 * colors[observed] + .15 * sums[observed] / weight_sum[observed, None]
    texture = np.zeros((2048, 2048, 3), np.uint8)
    texture[yy, xx] = colors.clip(0, 255).astype(np.uint8)
    observed_image = np.zeros((2048, 2048), bool)
    observed_image[yy[observed], xx[observed]] = True
    # Nearest observed color conditions unused chart padding without counting it as observed.
    _, labels = cv2.distanceTransformWithLabels((~observed_image).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    nearest_palette = texture[observed_image]
    texture[~observed_image] = nearest_palette[(labels[~observed_image] - 1).clip(0, len(nearest_palette) - 1)]
    missing = used & ~observed_image
    check()
    texture, inpaint_report = inpaint_texture(texture, missing, backend=inpainting, device=device)
    # Smooth only best-view chart seams, retaining measured texture elsewhere.
    ids_image = np.full((2048, 2048), -1, np.float32)
    ids_image[yy, xx] = source_ids
    seam = observed_image & ((cv2.dilate(ids_image, np.ones((3, 3), np.uint8)) - cv2.erode(ids_image, np.ones((3, 3), np.uint8))) > 0)
    smooth = cv2.GaussianBlur(texture, (3, 3), .6)
    texture[seam] = smooth[seam]
    fraction = float(missing.sum() / used.sum())
    return (vertices, normals, uv, faces, texture), {
        "atlas_size": [2048, 2048], "surface_texels": int(used.sum()),
        "observed_texel_fraction": 1 - fraction, "inpainted_texel_fraction": fraction,
        "low_quality": fraction > .4, "inpainting": inpaint_report,
        "view_selection": f"{visibility}-tested best view, weighted seam blend",
        "visibility_source": visibility,
    }
