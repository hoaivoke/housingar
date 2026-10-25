from dataclasses import dataclass, field
import numpy as np

from ..captures.geometry import gravity_frame, footprint_frame


@dataclass
class Candidate:
    proposal_id: str
    points: np.ndarray
    parent_id: str | None = None
    support_height: float | None = None
    excluded_planes: list = field(default_factory=list)


def clusters(points, epsilon=.035, minimum=35):
    import open3d as o3d
    if len(points) < minimum:
        return []
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    labels = np.asarray(cloud.cluster_dbscan(eps=epsilon, min_points=4, print_progress=False))
    return [points[labels == label] for label in sorted(set(labels)-{-1}) if (labels==label).sum() >= minimum]


def remove_room_planes(points, anchor_to_world, check):
    import open3d as o3d
    from scipy.spatial import ConvexHull
    basis = gravity_frame(anchor_to_world)
    up = basis[:, 1]
    aligned = points @ basis
    floor_height = float(np.quantile(aligned[:, 1], .005))
    bounds = np.quantile(aligned[:, [0,2]], [.005,.995], axis=0)
    remaining = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    planes = []
    o3d.utility.random.seed(73)
    for _ in range(14):
        check()
        if len(remaining.points) < 200:
            break
        coefficients, ids = remaining.segment_plane(distance_threshold=.008, ransac_n=3, num_iterations=250)
        p = np.asarray(remaining.points)[ids]
        remaining = remaining.select_by_index(ids, invert=True)
        normal = np.array(coefficients[:3]); normal /= np.linalg.norm(normal)
        value = abs(normal @ up)
        local = p @ basis
        _, _, axes = np.linalg.svd(p-p.mean(0), full_matrices=False)
        try: area = float(ConvexHull((p-p.mean(0)) @ axes[:2].T).volume)
        except Exception: continue
        floor = value > .97 and area > .5 and abs(np.median(local[:,1])-floor_height)<.035
        signed=points@normal-float(p.mean(0)@normal)
        edge=np.quantile(signed,[.005,.995])
        at_boundary=min(abs(edge[0]),abs(edge[1]))<.03
        wall = value < .12 and area > 1.5 and np.ptp(local[:,1]) > 1.5 and at_boundary
        if floor or wall:
            planes.append({'kind':'floor' if floor else 'wall','normal_anchor':normal.tolist(),'offset':float(-p.mean(0)@normal),'area_m2':area})
    keep = np.ones(len(points), bool)
    for plane in planes:
        keep &= np.abs(points @ plane['normal_anchor'] + plane['offset']) > .012
    return points[keep], planes, floor_height


def discover(points, anchor_to_world, check):
    import open3d as o3d
    basis = gravity_frame(anchor_to_world)
    foreground, planes, floor = remove_room_planes(points, anchor_to_world, check)
    candidates = []
    for group in clusters(foreground, epsilon=.04, minimum=90):
        check()
        extent = np.ptp(group @ footprint_frame(group, anchor_to_world), axis=0)
        if extent.max() > 2.5 or extent.max() < .05:
            continue
        parent_id = f'object_{len(candidates)+1:03d}'
        local = group @ basis
        # Strong horizontal occupancy levels reveal tabletops, while preserving
        # above-floor furniture rather than removing every horizontal plane.
        bins = np.floor(local[:,1]/.008).astype(int)
        values, counts = np.unique(bins, return_counts=True)
        peaks = values[counts > max(100, np.median(counts)*3)]
        supports = []
        for bin_id in peaks:
            height = (bin_id+.5)*.008
            near = np.abs(local[:,1]-height)<.009
            flat = local[near][:,[0,2]]
            if len(flat)<50: continue
            from scipy.spatial import ConvexHull
            try: area = ConvexHull(flat).volume
            except Exception: continue
            if area>.30 and height>floor+.25 and height<floor+1.5:
                supports.append((area,height))
        children = []
        support = max(supports)[1] if supports else None
        if support is not None:
            elevated = group[local[:,1] > support+.018]
            # Distinct top levels/components seed touching-item separation.
            top_seeds=[]
            if len(elevated):
                cloud=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(elevated))
                cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=.035,max_nn=35))
                horizontal=np.abs(np.asarray(cloud.normals)@basis[:,1])>.90
                for piece in clusters(elevated[horizontal],epsilon=.022,minimum=25):
                    q=piece @ basis
                    height=float(np.median(q[:,1]))
                    if height<support+.05:continue
                    flat=piece[np.abs(q[:,1]-height)<.006]@basis
                    if len(flat)<20 or min(np.ptp(flat[:,[0,2]],axis=0))<.045:continue
                    try:hull=ConvexHull(flat[:,[0,2]])
                    except Exception:continue
                    centre=np.median(flat[:,[0,2]],axis=0)
                    if any(np.linalg.norm(centre-seed[0])<.04 for seed in top_seeds):continue
                    top_seeds.append((centre,hull.equations,height))
            if len(top_seeds)>1:
                xy=(elevated @ basis)[:,[0,2]]
                distances=[]
                for centre,equations,height in top_seeds:
                    outside=np.maximum((xy@equations[:,:2].T+equations[:,2]).max(axis=1),0)
                    distance=outside+.001*np.linalg.norm(xy-centre,axis=1)
                    distance[(elevated@basis[:,1])>height+.008]=np.inf
                    distances.append(distance)
                assigned=np.argmin(distances,axis=0)
                distance=np.min(distances,axis=0)
                children=[elevated[(assigned==i)&(distance<.008)] for i in range(len(top_seeds)) if ((assigned==i)&(distance<.008)).sum()>=50]
            else:
                children=clusters(elevated,epsilon=.025,minimum=50)
        if children:
            desk = group[local[:,1] <= support+.012]
            candidates.append(Candidate(parent_id, desk, excluded_planes=planes))
            for child in children:
                size=np.ptp(child @ footprint_frame(child, anchor_to_world),axis=0)
                if size.max()<.05 or size.max()>2.5:continue
                support_plane={'normal_anchor':basis[:,1].tolist(),'offset':-support,'kind':'furniture_support'}
                candidates.append(Candidate(f'object_{len(candidates)+1:03d}',child,parent_id,support,[*planes,support_plane]))
        else:
            candidates.append(Candidate(parent_id,group,excluded_planes=planes))
    return candidates, {'removed_planes':planes,'floor_height_anchor_up_m':floor,'foreground_points':len(foreground)}
