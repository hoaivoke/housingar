import numpy as np

from .format import matrix
from .geometry import CV_TO_GL, anchor_depth_points, box_corners, depth_to_image, gravity_frame


def fuse_depth(capture, masks, check, *, object_only, voxel=.004, output=None, extent=None, diagnostic_height=None,silhouette_masks=None,minimum_confidence=64):
    import open3d as o3d
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel, sdf_trunc=voxel * 4,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    all_points = []
    observations = []
    observation_points=[]
    used = 0
    outside_seed, outside_refined, point_count = 0, 0, 0
    up=gravity_frame(capture.manifest.anchor_to_world)[:,1]
    reference=diagnostic_height if diagnostic_height is not None else (float((extent.low[1]+extent.high[1])/2) if extent is not None else 0.)
    evidence_stages={}
    def record(name,points):
        points=np.asarray(points);row=evidence_stages.setdefault(name,{'points':0,'above_reference':0,'above_reference_plus_5cm':0})
        row['points']+=len(points)
        if len(points):
            heights=points@up;row['above_reference']+=int((heights>reference).sum());row['above_reference_plus_5cm']+=int((heights>reference+.05).sum())
            row['min_height_m']=min(row.get('min_height_m',float('inf')),float(heights.min()));row['max_height_m']=max(row.get('max_height_m',float('-inf')),float(heights.max()))
    for frame, mask in zip(capture.manifest.frames, masks):
        check()
        depth = capture.depth(frame).copy()
        confidence = capture.confidence(frame)
        x, y = depth_to_image(frame)
        selected = mask[y, x] if object_only else ~mask[y, x]
        selected &= confidence >= minimum_confidence
        selected &= depth > 0
        points = anchor_depth_points(frame, depth)
        if object_only:record('masked_depth',points[selected])
        if extent is not None:
            if object_only:
                selected &= ~extent.on_support(points,frame_index=frame.index)
                record('support_rejected_depth',points[selected])
                evidence = points[selected]
                bounds = capture.manifest.seed.bbox_anchor
                outside_seed += int((~np.all((evidence >= bounds.min) & (evidence <= bounds.max), axis=1)).sum())
                outside_refined += int((~extent.contains(evidence)).sum())
                point_count += len(evidence)
                selected &= extent.contains(points)
                record('extent_clipped_depth',points[selected])
            else:
                # Do not fuse missed object/base-edge pixels into the background.
                # Only genuinely measured plane depth may survive inside its volume.
                selected &= ~extent.contains(points, .03) | extent.on_support(points, distance=.006,frame_index=frame.index)
        depth[~selected] = 0
        if np.count_nonzero(depth) < 20:
            continue
        all_points.append(points[selected][::3])
        if object_only:observation_points.append(points[selected])
        color = np.ascontiguousarray(capture.image(frame)[y, x])
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(o3d.geometry.Image(color), o3d.geometry.Image(depth), depth_scale=1000, depth_trunc=8., convert_rgb_to_intensity=False)
        k = frame.depth.intrinsics
        # Open3D takes integer pixel centres; our v1 continuous centres are i+0.5.
        intrinsic = o3d.camera.PinholeCameraIntrinsic(frame.depth.width, frame.depth.height, k.fx, k.fy, k.cx - .5, k.cy - .5)
        camera_cv_to_anchor = matrix(frame.camera_to_anchor) @ CV_TO_GL
        volume.integrate(rgbd, intrinsic, np.linalg.inv(camera_cv_to_anchor))
        observations.append((rgbd, intrinsic, np.linalg.inv(camera_cv_to_anchor)))
        used += 1
    mesh = volume.extract_triangle_mesh()
    implementation = "open3d_scalable_tsdf"
    actual_voxel = voxel
    if not len(mesh.triangles) and used:
        # Some arm64 Open3D wheels return an empty scalable volume even for a
        # constant-depth plane. The bounded uniform implementation remains TSDF.
        bounds = extent.bounds if extent is not None else capture.manifest.seed.bbox_anchor
        centre = (np.asarray(bounds.min) + bounds.max) / 2
        length = float(np.max(np.asarray(bounds.max) - bounds.min) + .10) if object_only else 3.4
        resolution = min(256, int(np.ceil(length / voxel)))
        actual_voxel = length / resolution
        volume = o3d.pipelines.integration.UniformTSDFVolume(length=length, resolution=resolution,
            sdf_trunc=actual_voxel * 4, color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
            origin=centre - length / 2)
        for rgbd, intrinsic, extrinsic in observations:
            check()
            volume.integrate(rgbd, intrinsic, extrinsic)
        mesh = volume.extract_triangle_mesh()
        implementation = "open3d_uniform_tsdf_fallback_empty_scalable"
    mesh.remove_degenerate_triangles().remove_duplicated_triangles().remove_duplicated_vertices().remove_unreferenced_vertices()
    if not len(mesh.triangles):
        raise ValueError("Depth fusion produced no triangles; scan more sides with reliable depth")
    if object_only:record('tsdf_surface_vertices',np.asarray(mesh.vertices))
    if object_only:
        bounds = extent.bounds if extent is not None else capture.manifest.seed.bbox_anchor
        triangles_before_extent = len(mesh.triangles)
        if extent is not None:
            transform = np.eye(4); transform[:3, :3] = extent.basis.T
            mesh.transform(transform)
            mesh = mesh.crop(o3d.geometry.AxisAlignedBoundingBox(extent.low, extent.high))
            mesh.transform(np.linalg.inv(transform))
        else:
            mesh = mesh.crop(o3d.geometry.AxisAlignedBoundingBox(np.asarray(bounds.min) - voxel, np.asarray(bounds.max) + voxel))
        if not len(mesh.triangles):
            raise ValueError("Fused object falls outside the selected 3D extent")
        extent_removed=triangles_before_extent-len(mesh.triangles)
        record('clipped_surface_vertices',np.asarray(mesh.vertices))
        # Thin chair rails/legs can be separate TSDF islands. Keep islands
        # backed by independent masked depth views rather than only the
        # largest (usually the seat or a leaked floor patch).
        labels, counts, areas = mesh.cluster_connected_triangles()
        component_report={}
        triangles_before_components=len(mesh.triangles)
        if len(counts):
            from scipy.spatial import cKDTree
            labels=np.asarray(labels);counts=np.asarray(counts);vertices=np.asarray(mesh.vertices);faces=np.asarray(mesh.triangles)
            centres=vertices[faces].mean(1);sample_ids=[]
            for label,count in enumerate(counts):
                if count<8:continue
                ids=np.flatnonzero(labels==label);sample_ids.extend(ids[::max(1,len(ids)//64)][:64])
            sample_ids=np.asarray(sample_ids,int);view_counts=np.zeros(len(counts),int)
            if len(sample_ids):
                for points in observation_points:
                    check();near=cKDTree(points).query(centres[sample_ids])[0]<max(.015,actual_voxel*3)
                    hits=np.bincount(labels[sample_ids[near]],minlength=len(counts));view_counts+=hits>=2
            keep=(counts>=8)&(view_counts>=2)
            component_report={'total':len(counts),'retained':int(keep.sum()),'minimum_triangles':8,'minimum_depth_views':2,
                'validation_distance_m':max(.015,actual_voxel*3),'largest_only':False}
            mesh.remove_triangles_by_mask(~keep[labels])
            mesh.remove_unreferenced_vertices()
        components_removed=triangles_before_components-len(mesh.triangles)
        record('component_validated_surface_vertices',np.asarray(mesh.vertices))
        # A single noisy floor must not survive as horizontal caps/fragments
        # between historical RANSAC bands. Vertical leg sides remain eligible.
        floor_mesh_validation={}
        floor_triangles_removed=reject_floor_fragments(mesh,extent,capture=capture,silhouette_masks=silhouette_masks,check=check,audit=floor_mesh_validation)
        if not len(mesh.triangles):raise ValueError('No object surface has support from independent depth views')
        record('retained_surface_vertices',np.asarray(mesh.vertices))
    mesh.compute_vertex_normals()
    if output:
        o3d.io.write_triangle_mesh(str(output), mesh)
    return mesh, {"frames_used": used, "triangles": len(mesh.triangles), "voxel_m": actual_voxel,
                  "minimum_confidence": minimum_confidence,
                  "bbox_constraint_padding_m": (0. if extent is not None else voxel) if object_only else None,
                  "triangles_removed_by_extent": extent_removed if object_only else 0,
                  "triangles_removed_by_components":components_removed if object_only else 0,
                  "fused_points": point_count if object_only else None,
                  "outside_seed_fraction": outside_seed / max(1, point_count) if object_only else None,
                  "outside_refined_fraction": outside_refined / max(1, point_count) if object_only else None,
                  "implementation": implementation,
                  "component_validation":component_report if object_only else None,
                  "floor_triangles_removed":floor_triangles_removed if object_only else None,
                  "floor_mesh_validation":floor_mesh_validation if object_only else None,
                  "geometry_evidence":{'reference_height_m':reference,'stages':evidence_stages} if object_only else None}, np.concatenate(all_points) if all_points else None


def reject_floor_fragments(mesh,extent,*,capture=None,silhouette_masks=None,check=lambda:None,audit=None):
    if extent is None or not len(mesh.triangles):return 0
    mesh.compute_triangle_normals();vertices=np.asarray(mesh.vertices);faces=np.asarray(mesh.triangles);centres=vertices[faces].mean(1);normals=np.asarray(mesh.triangle_normals)
    floor=np.zeros(len(faces),bool)
    for plane in extent.planes:
        if plane.get('kind')!='floor':continue
        n=np.asarray(plane['normal_anchor']);signed=centres@n+plane['offset'];band=plane.get('rejection_distance_m',.015)
        if extent.floor_visibility_exclusions and capture is not None and silhouette_masks is not None:
            from .visibility import silhouette_consistency
            ids=np.flatnonzero(signed<band+.15)
            if len(ids):
                rejected,report=silhouette_consistency(capture,centres[ids],silhouette_masks,check)
                floor[ids]|=rejected
                if audit is not None:audit.update(report)
            # Horizontal wood foot rails can lie inside a broad noisy-floor
            # envelope. Verified silhouettes preserve them above the core.
            band=.015
        floor|=(signed<band)&(np.abs(normals@n)>np.cos(np.deg2rad(35)))
    removed=int(floor.sum());mesh.remove_triangles_by_mask(floor);mesh.remove_unreferenced_vertices()
    return removed


def simplify(mesh, maximum=50000):
    if len(mesh.triangles) > maximum:
        # A global quadric budget can erase complete small backrest/leg
        # islands. Reserve a small budget for each validated component.
        labels,counts,_=mesh.cluster_connected_triangles();counts=np.asarray(counts);base=np.minimum(counts,12)
        reserve=2*int((counts>12).sum());available=maximum-reserve
        if len(counts)>1 and base.sum()<available:
            import open3d as o3d
            vertices=np.asarray(mesh.vertices);faces=np.asarray(mesh.triangles)
            extra=counts-base;budgets=base+np.floor((available-base.sum())*extra/max(1,extra.sum())).astype(int)
            order=np.argsort(np.asarray(labels),kind='stable');groups=np.split(order,np.cumsum(counts)[:-1]);result=o3d.geometry.TriangleMesh()
            for ids,budget in zip(groups,budgets):
                unique,inverse=np.unique(faces[ids].reshape(-1),return_inverse=True)
                part=o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices[unique]),o3d.utility.Vector3iVector(inverse.reshape(-1,3)))
                if len(part.triangles)>budget:part=part.simplify_quadric_decimation(int(budget))
                result+=part
            mesh=result
        if len(mesh.triangles)>maximum:mesh=mesh.simplify_quadric_decimation(maximum)
    mesh.remove_degenerate_triangles().remove_unreferenced_vertices()
    mesh.compute_vertex_normals()
    return mesh


def background_patch(capture, mesh, cloud, check, *, margin=.10, max_extension=.20, extent=None):
    """Local supported plane completion, plus observed nonplanar TSDF surfaces."""
    import open3d as o3d
    from scipy.spatial import ConvexHull, Delaunay, cKDTree
    bounds = extent.bounds if extent is not None else capture.manifest.seed.bbox_anchor
    centre = (np.asarray(bounds.min) + bounds.max) / 2
    up = gravity_frame(capture.manifest.anchor_to_world)[:, 1]
    cloud = cloud[np.linalg.norm(cloud - centre, axis=1) < 2.5]
    point_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(cloud)).voxel_down_sample(.008)
    result = o3d.geometry.TriangleMesh()
    planes = []
    corners = extent.corners if extent is not None else box_corners(bounds)
    for _ in range(6):
        check()
        if len(point_cloud.points) < 150:
            break
        coefficients, inliers = point_cloud.segment_plane(distance_threshold=.009, ransac_n=3, num_iterations=300)
        points = np.asarray(point_cloud.points)[inliers]
        point_cloud = point_cloud.select_by_index(inliers, invert=True)
        if len(points) < 100:
            continue
        normal = np.asarray(coefficients[:3])
        normal /= np.linalg.norm(normal)
        if np.dot(normal, centre - points.mean(0)) < 0:
            normal = -normal
        # Support geometry must be near the selected volume, not arbitrary distant planes.
        if abs(np.dot(normal, centre - points.mean(0))) > 1.8:
            continue
        origin = points.mean(0)
        first = np.cross(normal, up)
        if np.linalg.norm(first) < .1:
            first = gravity_frame(capture.manifest.anchor_to_world)[:, 0]
        first /= np.linalg.norm(first)
        second = np.cross(normal, first)
        basis = np.column_stack([first, second])
        support_uv = (points - origin) @ basis
        try:
            if ConvexHull(support_uv).volume < .15:
                continue  # A tiny planar object face is not a room support surface.
        except Exception:
            continue
        signed_corners = (corners - origin) @ normal
        if signed_corners.min() < -.012 and signed_corners.max() > .012:
            continue  # Never extend a plane through the selected object's volume.
        uv = (corners - origin) @ basis
        projected = [uv]
        for frame in capture.manifest.frames:
            camera = matrix(frame.camera_to_anchor)[:3, 3]
            rays = corners - camera
            denominator = rays @ normal
            numerator = np.dot(origin - camera, normal)
            t = np.divide(numerator, denominator, out=np.full(8, -1.), where=np.abs(denominator) > 1e-5)
            hits = camera + t[:, None] * rays
            good = (t >= .99) & (t < 5) & (np.linalg.norm(hits - centre, axis=1) < 2)
            if np.any(good):
                projected.append((hits[good] - origin) @ basis)
        region = np.concatenate(projected)
        try:
            hull = ConvexHull(region)
            region = region[hull.vertices]
            triangulation = Delaunay(region)
        except Exception:
            continue
        lo = np.maximum(region.min(0) - margin, support_uv.min(0) - max_extension)
        hi = np.minimum(region.max(0) + margin, support_uv.max(0) + max_extension)
        step = .012
        nx, ny = np.ceil((hi - lo) / step).astype(int) + 1
        if nx < 2 or ny < 2 or nx * ny > 120000:
            continue
        yy, xx = np.indices((ny, nx))
        grid_uv = np.stack([lo[0] + xx * step, lo[1] + yy * step], -1).reshape(-1, 2)
        # Margin around projected silhouette; do not extend >20 cm from observed support.
        distance = cKDTree(support_uv).query(grid_uv)[0]
        in_region = triangulation.find_simplex(grid_uv) >= 0
        near_region = cKDTree(region).query(grid_uv)[0] < margin
        valid = (in_region | near_region) & (distance <= max_extension)
        vertices = origin + grid_uv @ basis.T
        cells = (yy[:-1, :-1] * nx + xx[:-1, :-1]).reshape(-1)
        quads = np.stack([cells, cells + 1, cells + nx, cells + nx + 1], -1)
        quads = quads[valid[quads].all(axis=1)]
        triangles = np.concatenate([quads[:, [0, 1, 2]], quads[:, [1, 3, 2]]])
        if not len(triangles):
            continue
        patch = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices), o3d.utility.Vector3iVector(triangles))
        patch.remove_unreferenced_vertices().compute_vertex_normals()
        if np.mean(np.asarray(patch.triangle_normals) @ normal) < 0:
            patch.triangles = o3d.utility.Vector3iVector(np.asarray(patch.triangles)[:, [0, 2, 1]])
        result += patch
        planes.append({"normal_anchor": normal.tolist(), "offset": float(-np.dot(normal, origin)),
                       "observed_samples": len(points), "max_extension_m": max_extension})
    # Preserve measured nonplanar surfaces in the immediate neighborhood.
    vertices, faces = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    centres = vertices[faces].mean(1)
    lo, hi = np.asarray(bounds.min) - margin, np.asarray(bounds.max) + margin
    keep = np.all((centres >= lo) & (centres <= hi), axis=1)
    if extent is not None:
        keep &= ~extent.contains(centres, .03)
    for plane in planes:
        keep &= np.abs(centres @ plane["normal_anchor"] + plane["offset"]) > .015
    if keep.any():
        observed = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices.copy()), o3d.utility.Vector3iVector(faces[keep]))
        observed.remove_unreferenced_vertices()
        result += observed
    if not len(result.triangles):
        raise ValueError("No nearby observed support surfaces for a safe 3D background patch")
    result = simplify(result)
    return result, {"support_planes": planes, "margin_m": margin,
                    "max_extension_m": max_extension, "completion": "observed TSDF plus locally supported planes"}
