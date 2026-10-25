"""Explicit OpenGL-camera/anchor/OpenCV conversions; all distances are metres."""
import numpy as np

from .format import matrix

CV_TO_GL = np.diag([1., -1., -1., 1.])


def transform_points(points, transform):
    return np.asarray(points) @ transform[:3, :3].T + transform[:3, 3]


def depth_coordinates(frame):
    v, u = np.indices((frame.depth.height, frame.depth.width), dtype=np.float64)
    return u + 0.5, v + 0.5


def depth_to_image(frame):
    u, v = depth_coordinates(frame)
    sx, sy, tx, ty = frame.depth.to_image
    return np.floor(sx * u + tx).astype(int).clip(0, frame.width - 1), np.floor(sy * v + ty).astype(int).clip(0, frame.height - 1)


def anchor_depth_points(frame, depth):
    u, v = depth_coordinates(frame)
    z = depth.astype(np.float64) / 1000
    k = frame.depth.intrinsics
    camera = np.stack([z * (u - k.cx) / k.fx, z * (k.cy - v) / k.fy, -z], axis=-1)
    return transform_points(camera.reshape(-1, 3), matrix(frame.camera_to_anchor)).reshape(*depth.shape, 3)


def project(points_anchor, frame):
    camera = transform_points(points_anchor, np.linalg.inv(matrix(frame.camera_to_anchor)))
    z = -camera[:, 2]
    k = frame.intrinsics
    safe = np.maximum(z, 1e-8)
    return np.stack([k.fx * camera[:, 0] / safe + k.cx, k.cy - k.fy * camera[:, 1] / safe], axis=-1), z


def box_corners(bounds):
    return np.asarray([[x, y, z] for x in (bounds.min[0], bounds.max[0])
                       for y in (bounds.min[1], bounds.max[1]) for z in (bounds.min[2], bounds.max[2])])


def projected_box(points, frame):
    pixels, depth = project(points, frame)
    pixels = pixels[depth > 0.01]
    if not len(pixels):
        return None
    lo, hi = pixels.min(axis=0), pixels.max(axis=0)
    lo = np.maximum(lo, 0)
    hi = np.minimum(hi, [frame.width - 1, frame.height - 1])
    if np.any(hi <= lo):
        return None
    return np.r_[lo, hi].astype(np.float32)


def depth_bbox_mask(capture, frame, expansion=0.0, extent=None):
    import cv2
    depth = capture.depth(frame)
    points = anchor_depth_points(frame, depth)
    bounds = capture.manifest.seed.bbox_anchor
    good = (depth > 0) & (capture.confidence(frame) >= 64)
    inside = (good & extent.contains(points, expansion) & ~extent.on_support(points,frame_index=frame.index)) if extent is not None else (good & np.all(points >= np.asarray(bounds.min) - expansion, axis=-1) & np.all(points <= np.asarray(bounds.max) + expansion, axis=-1))
    yy, xx = np.indices((frame.height, frame.width), dtype=np.float32)
    sx, sy, tx, ty = frame.depth.to_image
    map_x = ((xx + .5 - tx) / sx - .5).astype(np.float32)
    map_y = ((yy + .5 - ty) / sy - .5).astype(np.float32)
    full = cv2.remap(inside.astype(np.uint8), map_x, map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT) > 0
    valid = cv2.remap(good.astype(np.uint8), map_x, map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT) > 0
    return full, valid


def umeyama(source, target):
    source, target = np.asarray(source), np.asarray(target)
    if source.shape != target.shape or source.shape[0] < 3 or np.linalg.matrix_rank(source - source.mean(0)) < 2:
        raise ValueError("Similarity registration needs at least three non-collinear camera centres")
    a, b = source - source.mean(0), target - target.mean(0)
    u, singular, vt = np.linalg.svd(b.T @ a / len(a))
    sign = np.ones(3)
    sign[-1] = np.sign(np.linalg.det(u @ vt))
    rotation = u @ np.diag(sign) @ vt
    scale = float(np.sum(singular * sign) / np.mean(np.sum(a * a, axis=1)))
    transform = np.eye(4)
    transform[:3, :3] = scale * rotation
    transform[:3, 3] = target.mean(0) - scale * rotation @ source.mean(0)
    return transform, scale


def register_camera_poses(source, target, threshold=.03, iterations=300):
    source, target = np.asarray(source), np.asarray(target)
    rng = np.random.default_rng(42)
    best = np.zeros(len(source), bool)
    if len(source) < 6:
        raise ValueError("Fewer than six reconstructed/ARCore camera correspondences")
    for _ in range(iterations):
        ids = rng.choice(len(source), 3, replace=False)
        try:
            estimate, _ = umeyama(source[ids], target[ids])
        except ValueError:
            continue
        inliers = np.linalg.norm(transform_points(source, estimate) - target, axis=1) < threshold
        if inliers.sum() > best.sum():
            best = inliers
    if best.sum() < max(6, int(np.ceil(len(source) * .7))):
        raise ValueError("Object Capture camera registration has too few consistent inliers")
    transform, scale = umeyama(source[best], target[best])
    errors = np.linalg.norm(transform_points(source, transform) - target, axis=1)
    rms = float(np.sqrt(np.mean(errors[best] ** 2)))
    if not .001 < scale < 1000 or rms > threshold:
        raise ValueError("Object Capture registration scale/RMS failed quality checks")
    return transform, {"rms_m": rms, "scale": scale, "inliers": int(best.sum()), "correspondences": len(source)}


def gravity_frame(anchor_to_world):
    up = matrix(anchor_to_world)[:3, :3].T @ [0., 1., 0.]
    up /= np.linalg.norm(up)
    right = np.array([1., 0., 0.])
    right -= np.dot(right, up) * up
    if np.linalg.norm(right) < .1:
        right = np.cross(up, [0., 0., 1.])
    right /= np.linalg.norm(right)
    back = np.cross(right, up)
    return np.column_stack([right, up, back])


def object_local_frame(vertices, anchor_to_world):
    basis = footprint_frame(vertices, anchor_to_world)
    aligned = np.asarray(vertices) @ basis
    lo, hi = aligned.min(0), aligned.max(0)
    bottom = np.array([(lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2])
    object_to_anchor = np.eye(4)
    object_to_anchor[:3, :3] = basis
    object_to_anchor[:3, 3] = basis @ bottom
    return aligned - bottom, object_to_anchor, hi - lo


def footprint_frame(points, anchor_to_world):
    """Minimum-area gravity-plane rectangle, long horizontal side is local +X."""
    import cv2
    base = gravity_frame(anchor_to_world)
    footprint = (np.asarray(points) @ base)[:, [0, 2]].astype(np.float32)
    rectangle = cv2.boxPoints(cv2.minAreaRect(footprint))
    edges = np.roll(rectangle, -1, axis=0) - rectangle
    lengths = np.linalg.norm(edges, axis=1)
    edge = edges[int(np.argmax(lengths))]
    if np.linalg.norm(edge) < 1e-8:
        return base
    edge = edge / np.linalg.norm(edge)
    right = base[:, 0] * edge[0] + base[:, 2] * edge[1]
    if np.dot(right, base[:, 0]) < -1e-7 or (abs(np.dot(right, base[:, 0])) <= 1e-7 and np.dot(right, base[:, 2]) < 0):
        right = -right
    return np.column_stack([right, base[:, 1], np.cross(right, base[:, 1])])
