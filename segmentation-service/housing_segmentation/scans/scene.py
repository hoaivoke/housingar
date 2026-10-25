"""CPU confidence-weighted sparse projective TSDF and observed zero samples."""
from pathlib import Path
import numpy as np

from ..captures.geometry import anchor_depth_points
from ..captures.format import matrix

BIAS = 1 << 20
BASE = 1 << 21


def keys_for(points, voxel):
    grid = np.floor(points / voxel).astype(np.int64) + BIAS
    if np.any((grid < 0) | (grid >= BASE)):
        raise ValueError('Scan exceeds supported voxel coordinate range')
    return (grid[:, 0] * BASE + grid[:, 1]) * BASE + grid[:, 2]


def weighted_reduce(keys, values, weights):
    unique, inverse = np.unique(keys, return_inverse=True)
    weight_sum = np.bincount(inverse, weights=weights)
    values = np.asarray(values)
    if values.ndim == 1:
        total = np.bincount(inverse, weights=values * weights)
        return unique, total / np.maximum(weight_sum, 1e-12), weight_sum
    total = np.stack([np.bincount(inverse, weights=values[:, i] * weights) for i in range(values.shape[1])], 1)
    return unique, total / np.maximum(weight_sum[:, None], 1e-12), weight_sum


def fuse_scene(scan, work, check, *, voxel=.008):
    import open3d as o3d
    all_points, all_weights = [], []
    keys, values, weights = [], [], []
    blocks = []
    offsets = np.arange(-3, 4) * voxel
    observations = 0
    for position, frame in enumerate(scan.manifest.frames):
        check()
        depth = scan.depth(frame)
        confidence = scan.confidence(frame)
        valid = (depth > 0) & (depth < 8000) & (confidence >= 32)
        points = anchor_depth_points(frame, depth)[valid]
        z = depth[valid].astype(float) / 1000
        weight = confidence[valid].astype(float) / 255
        if len(points) == 0:
            continue
        all_points.append(points); all_weights.append(weight)
        camera = matrix(frame.camera_to_anchor)[:3, 3]
        forward = -matrix(frame.camera_to_anchor)[:3, 2]
        ray = (points-camera) / z[:, None]
        for offset in offsets:
            samples=points+offset*ray
            keys.append(keys_for(samples, voxel))
            centres=(np.floor(samples/voxel)+.5)*voxel
            voxel_z=(centres-camera)@forward
            values.append(np.clip((z-voxel_z)/(3*voxel),-1,1).astype(np.float32))
            weights.append(weight.astype(np.float32))
        observations += len(points)
        if len(keys) >= 70 or position == len(scan.manifest.frames)-1:
            blocks.append(weighted_reduce(np.concatenate(keys), np.concatenate(values), np.concatenate(weights)))
            keys, values, weights = [], [], []
    if keys:
        blocks.append(weighted_reduce(np.concatenate(keys), np.concatenate(values), np.concatenate(weights)))
    if not blocks:
        raise ValueError('Scan has no reliable depth to fuse')
    check()
    keys, sdf, weight = weighted_reduce(np.concatenate([b[0] for b in blocks]), np.concatenate([b[1] for b in blocks]), np.concatenate([b[2] for b in blocks]))
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(work/'scene-tsdf.npz', keys=keys, tsdf=sdf.astype(np.float32), weight=weight.astype(np.float32), voxel_m=voxel, truncation_m=3*voxel)
    # Retain confidence-weighted observed zero-distance samples for discovery;
    # this avoids treating unobserved voxel boundaries as measured surfaces.
    points, point_weights = np.concatenate(all_points), np.concatenate(all_weights)
    _, surface, surface_weights = weighted_reduce(keys_for(points, voxel), points, point_weights)
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(surface))
    o3d.io.write_point_cloud(str(work/'scene-observed.ply'), cloud)
    return surface, {'implementation':'sparse_projective_tsdf_cpu', 'voxel_m':voxel, 'truncation_m':3*voxel,
                     'confidence_weight':'uint8_confidence / 255; reject <32', 'depth_observations':observations,
                     'tsdf_voxels':len(keys), 'observed_surface_voxels':len(surface),
                     'surface_source':'confidence-weighted observed zero-distance samples',
                     'weight_range':[float(weight.min()),float(weight.max())]}
