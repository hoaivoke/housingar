"""Independent metric checks matching the phone fixture's published targets."""
import numpy as np

from .format import matrix
from .geometry import transform_points
from .glb import read_glb, read_accessor


def assess_geometry(vertices_anchor, background_vertices, background_faces, anchor_to_world, truth, *, object_to_anchor=None, low_quality=False):
    import open3d as o3d
    from scipy.spatial import cKDTree
    a2w = matrix(anchor_to_world)
    vertices = transform_points(vertices_anchor, a2w)
    low, high = np.array(truth['object_min_world']), np.array(truth['object_max_world'])
    minimum, maximum = vertices.min(0), vertices.max(0)
    corners = np.array([[x, high[1], z] for x in (low[0], high[0]) for z in (low[2], high[2])])
    distances = cKDTree(vertices).query(corners)[0]
    background = transform_points(background_vertices, a2w)
    scene = o3d.t.geometry.RaycastingScene(nthreads=1)
    mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(background), o3d.utility.Vector3iVector(background_faces))
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    xs = np.linspace(low[0] + .02, high[0] - .02, 10)
    zs = np.linspace(low[2] + .02, high[2] - .02, 10)
    origins = np.array([[x, high[1] + .3, z] for x in xs for z in zs])
    directions = np.tile([0, -1, 0], (100, 1))
    hit = scene.cast_rays(o3d.core.Tensor(np.c_[origins, directions].astype(np.float32)), nthreads=1)['t_hit'].numpy()
    heights = origins[:, 1] - hit
    covered = int((np.isfinite(hit) & (np.abs(heights - low[1]) < .03)).sum())
    blocked = int((np.isfinite(hit) & (heights > low[1] + .03)).sum())
    bottom_world = transform_points(np.zeros((1, 3)), a2w @ object_to_anchor)[0] if object_to_anchor is not None else np.array([(minimum[0] + maximum[0]) / 2, minimum[1], (minimum[2] + maximum[2]) / 2])
    placement_error = float(np.linalg.norm(bottom_world - [(low[0]+high[0])/2, low[1], (low[2]+high[2])/2]))
    bounds_pass = bool(np.all(minimum - low >= -.015) and np.all(minimum - low <= [.015, .025, .015]) and np.all(np.abs(maximum-high) <= .015))
    inside = np.all((background > low + .006) & (background < high - .006), axis=1)
    removal_corners = None
    if object_to_anchor is not None:
        object_world = a2w @ object_to_anchor
        local_vertices = transform_points(vertices, np.linalg.inv(object_world))
        truth_corners = np.array([[x,y,z] for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])])
        local_truth = transform_points(truth_corners, np.linalg.inv(object_world))
        removal_corners = int(np.all((local_truth >= local_vertices.min(0)-.041) & (local_truth <= local_vertices.max(0)+.041), axis=1).sum())
    checks = {'world_bounds': bounds_pass, 'top_corners': bool(np.all(distances < .02)),
              'placement': placement_error < .03, 'footprint_support': covered >= 95,
              'no_patch_inside_object': blocked == 0 and not inside.any(), 'quality': not low_quality,
              'removal_region': removal_corners is None or removal_corners == 8}
    return {'passes': all(checks.values()), 'checks': checks, 'world_min': minimum.tolist(), 'world_max': maximum.tolist(),
            'world_min_error_m': (minimum-low).tolist(), 'world_max_error_m': (maximum-high).tolist(),
            'top_corner_distances_m': distances.tolist(), 'top_corners_reached': int((distances < .02).sum()),
            'placement_error_m': placement_error, 'footprint_rays': 100, 'footprint_support_hits': covered,
            'footprint_inside_object_hits': blocked, 'background_vertices_inside_object': int(inside.sum()),
            'truth_corners_inside_removal_region': removal_corners}


def assess_outputs(job_folder, capture, truth, report):
    document, binary = read_glb(job_folder / 'outputs/object.glb')
    local = read_accessor(document, binary, 0)
    transform = matrix(report['object_to_anchor'])
    vertices = transform_points(local, transform)
    document, binary = read_glb(job_folder / 'outputs/background.glb')
    result = assess_geometry(vertices, read_accessor(document, binary, 0), read_accessor(document, binary, 3).reshape(-1, 3),
                             capture.manifest.anchor_to_world, truth, object_to_anchor=transform, low_quality=report['low_quality'])
    result['object_dimensions_m'] = report['object_dimensions_m']
    result['fused_object_points_outside_seed_fraction'] = report['fused_object_points_outside_seed_fraction']
    result['fused_object_points_outside_refined_fraction'] = report['fused_object_points_outside_refined_fraction']
    return result
