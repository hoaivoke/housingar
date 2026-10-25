"""Multi-view object evidence; the phone's single-view bbox is only a prior."""
from dataclasses import dataclass,field
import base64
from types import SimpleNamespace

import numpy as np

from .geometry import anchor_depth_points, depth_to_image, footprint_frame, gravity_frame

PLANE_DISTANCE = .015


@dataclass
class RefinedExtent:
    basis: np.ndarray
    low: np.ndarray
    high: np.ndarray
    planes: list
    floor_visibility_exclusions:dict=field(default_factory=dict,repr=False)

    def contains(self, points, padding=0):
        aligned = np.asarray(points) @ self.basis
        return np.all((aligned >= self.low - padding) & (aligned <= self.high + padding), axis=-1)

    def on_support(self, points, distance=None, frame_index=None):
        found = np.zeros(np.asarray(points).shape[:-1], bool)
        for plane in self.planes:
            observed=plane.get('frame_observations',{}).get(str(frame_index),plane)
            band=distance if distance is not None else observed.get('rejection_distance_m',PLANE_DISTANCE)
            signed=np.asarray(points)@observed['normal_anchor']+observed['offset']
            found |= signed<=band if plane.get('kind')=='floor' and distance is None else np.abs(signed)<=band
        excluded=self.floor_visibility_exclusions.get(frame_index)
        if excluded is not None and found.shape==excluded.shape:found|=excluded
        return found

    @property
    def corners(self):
        return np.array([[x, y, z] for x in (self.low[0], self.high[0]) for y in (self.low[1], self.high[1]) for z in (self.low[2], self.high[2])]) @ self.basis.T

    @property
    def bounds(self):
        return SimpleNamespace(min=self.corners.min(0), max=self.corners.max(0))

    @classmethod
    def from_report(cls, report):
        exclusions={}
        for index,record in report.get('floor_visibility_exclusions',{}).items():
            shape=tuple(record['shape']);size=int(np.prod(shape));packed=np.frombuffer(base64.b64decode(record['bits_b64']),np.uint8)
            if len(shape)!=2 or size<=0 or len(packed)!=(size+7)//8:raise ValueError('Invalid floor visibility exclusion map')
            exclusions[int(index)]=np.unpackbits(packed)[:size].reshape(shape).astype(bool)
        return cls(np.array(report['basis_to_anchor']), np.array(report['min_local']), np.array(report['max_local']), report['support_planes'],exclusions)


def merge_support_planes(planes,cloud,up):
    """Collapse repeated RANSAC slices of one noisy surface, not floor bands."""
    groups=[];up=np.asarray(up)
    for plane in planes:
        normal=np.asarray(plane['normal_anchor']);offset=plane['offset']
        if normal@up<0 or (abs(normal@up)<.1 and normal[np.argmax(np.abs(normal))]<0):normal=-normal;offset=-offset
        value={**plane,'normal_anchor':normal.tolist(),'offset':float(offset)}
        for group in groups:
            first=group[0];n=np.asarray(first['normal_anchor'])
            if normal@n>np.cos(np.deg2rad(8)) and abs(offset-first['offset'])<.12:
                group.append(value);break
        else:groups.append([value])
    merged=[]
    for group in groups:
        weights=np.array([p['observed_samples'] for p in group]);normal=np.average([p['normal_anchor'] for p in group],axis=0,weights=weights);normal/=np.linalg.norm(normal)
        selected=np.zeros(len(cloud),bool)
        for plane in group:selected|=np.abs(cloud@plane['normal_anchor']+plane['offset'])<.015
        points=cloud[selected]
        offset=float(-np.median(points@normal)) if len(points) else group[0]['offset']
        residual=points@normal+offset
        band=max(PLANE_DISTANCE,float(np.quantile(np.abs(residual),.99))) if len(group)>1 and len(points) else PLANE_DISTANCE
        merged.append({'normal_anchor':normal.tolist(),'offset':offset,'observed_samples':len(points),
            'support_area_m2':max(p['support_area_m2'] for p in group),'merged_plane_count':len(group),
            'rejection_distance_m':band,'residual_p01_p99_m':np.quantile(residual,[.01,.99]).tolist() if len(points) else [0.,0.]})
    horizontal=[p for p in merged if np.asarray(p['normal_anchor'])@up>np.cos(np.deg2rad(12))]
    if horizontal:min(horizontal,key=lambda p:-p['offset']/(np.asarray(p['normal_anchor'])@up))['kind']='floor'
    return merged


def floor_frame_observations(plane,observations,frames,centre,up,check):
    """One floor, with local observational spread/offset for drifting frames."""
    if plane.get('kind')!='floor' or plane['rejection_distance_m']<=.025:return
    normal=np.asarray(plane['normal_anchor']);band=plane['rejection_distance_m'];profiles={}
    rng=np.random.default_rng(42)
    for (grid,good,selected),frame in zip(observations,frames):
        check();delta=grid-centre;horizontal=delta-(delta@up)[...,None]*up
        take=good&~selected&(np.linalg.norm(horizontal,axis=-1)<1.2)&(np.abs(grid@normal+plane['offset'])<band*1.5)
        points=grid[take][::2]
        if len(points)<40:continue
        best=np.zeros(len(points),bool)
        for _ in range(100):
            sample=points[rng.choice(len(points),3,replace=False)];n=np.cross(sample[1]-sample[0],sample[2]-sample[0]);length=np.linalg.norm(n)
            if length<1e-8:continue
            n/=length
            if n@up<0:n=-n
            if n@up<np.cos(np.deg2rad(10)):continue
            keep=np.abs((points-sample[0])@n)<.012
            if keep.sum()>best.sum():best=keep
        if best.sum()<30:continue
        origin=np.median(points[best],axis=0);_,_,axes=np.linalg.svd(points[best]-origin,full_matrices=False);n=axes[-1]
        if n@up<0:n=-n
        if n@up<np.cos(np.deg2rad(10)):continue
        offset=float(-np.median(points[best]@n));residual=points@n+offset
        sigma=float(1.4826*np.median(np.abs(residual-np.median(residual))))
        profiles[str(frame.index)]={'normal_anchor':n.tolist(),'offset':offset,'observed_samples':len(points),
            'rejection_distance_m':min(band,max(PLANE_DISTANCE,3*sigma)),'robust_sigma_m':sigma}
    plane['frame_observations']=profiles


def fit_support_planes(cloud, check, *, minimum_area=.18,up=None):
    import open3d as o3d
    from scipy.spatial import ConvexHull
    o3d.utility.random.seed(42)
    remaining = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(cloud)).voxel_down_sample(.009)
    planes = []
    for _ in range(10):
        check()
        if len(remaining.points) < 150:
            break
        coefficients, ids = remaining.segment_plane(distance_threshold=.006, ransac_n=3, num_iterations=400)
        points = np.asarray(remaining.points)[ids]
        remaining = remaining.select_by_index(ids, invert=True)
        if len(points) < 150:
            continue
        normal = np.asarray(coefficients[:3]); normal /= np.linalg.norm(normal)
        _, _, axes = np.linalg.svd(points - points.mean(0), full_matrices=False)
        try:
            area = ConvexHull((points - points.mean(0)) @ axes[:2].T).volume
        except Exception:
            continue
        # Room support must have broad measured extent. A small object face
        # observed when SAM misses a frame must never become a support plane.
        if area < minimum_area:
            continue
        planes.append({'normal_anchor': normal.tolist(), 'offset': float(-points.mean(0) @ normal),
                       'observed_samples': len(points), 'support_area_m2': float(area)})
    return merge_support_planes(planes,cloud,up) if up is not None else planes


def refine_extent(capture, masks, check, *, geometric_fallback=False, margin=.004, point_anchor=None, footprint_quantile=0.):
    """Fit support to unmasked depth, then a robust box to the selected component."""
    from scipy.spatial import cKDTree
    from scipy.sparse.csgraph import connected_components
    from scipy.sparse import coo_matrix
    observations, seed_points, background = [], [], []
    seed_index = capture.manifest.seed.frame_index
    for frame, mask in zip(capture.manifest.frames, masks):
        check()
        depth = capture.depth(frame)
        points = anchor_depth_points(frame, depth)
        x, y = depth_to_image(frame)
        good = (depth > 0) & (capture.confidence(frame) >= 64)
        selected = mask[y, x] & good
        observations.append((points, good, selected))
        if frame.index == seed_index:
            seed_points = points[selected]
        background.append(points[good & ~selected][::3])
    if len(seed_points) < 20:
        raise ValueError('Approved seed has too few reliable depth observations')
    centre = np.asarray(point_anchor) if point_anchor is not None else np.median(seed_points, axis=0)
    cloud = np.concatenate(background)
    cloud = cloud[np.linalg.norm(cloud - centre, axis=1) < 2.5]
    seed_basis = gravity_frame(capture.manifest.anchor_to_world)
    seed_limits = np.quantile(seed_points @ seed_basis, [.01, .99], axis=0)
    seed_spans = seed_limits[1] - seed_limits[0]
    minimum_area = max(.18, float(np.prod(np.sort(seed_spans)[-2:])) * 2)
    planes = fit_support_planes(cloud, check, minimum_area=minimum_area,up=seed_basis[:,1])
    if point_anchor is not None:
        # The tapped surface itself is object evidence, even when it is a
        # tabletop/seat. Do not classify a plane through that tap as support.
        planes=[p for p in planes if abs(np.asarray(point_anchor)@p['normal_anchor']+p['offset'])>.025]
    for plane in planes:floor_frame_observations(plane,observations,capture.manifest.frames,centre,seed_basis[:,1],check)
    exclusions={};visibility={}
    for plane in planes:
        if not geometric_fallback and plane.get('kind')=='floor' and plane.get('rejection_distance_m',PLANE_DISTANCE)>.025:
            from .visibility import floor_exclusions
            exclusions,visibility=floor_exclusions(capture,observations,masks,plane,check)
            break
    temporary = RefinedExtent(np.eye(3), np.zeros(3), np.ones(3), planes,exclusions)
    seed_position=next(i for i,f in enumerate(capture.manifest.frames) if f.index==seed_index)
    grid,good,selected=observations[seed_position]
    seed_points=grid[good&selected&~temporary.on_support(grid,frame_index=seed_index)]
    if len(seed_points) < 20:
        raise ValueError('Selected depth lies on fitted background/support planes')
    candidates = []
    for (points, good, selected),frame in zip(observations,capture.manifest.frames):
        take = good & (np.linalg.norm(points - centre, axis=-1) < .85) if geometric_fallback else selected
        candidates.append(points[take & ~temporary.on_support(points,frame_index=frame.index)])
    points = np.concatenate(candidates)
    if len(points) < 30:
        raise ValueError('Too few multi-view object depth points after support rejection')
    # Spatial connectivity keeps unrelated objects or isolated SAM outliers out
    # of the extent. Selection is tied to approved seed depth, not bbox volume.
    _, ids = np.unique(np.floor(points / .006).astype(np.int64), axis=0, return_index=True)
    samples = points[ids]
    pairs = cKDTree(samples).query_pairs(.025, output_type='ndarray')
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(samples), len(samples)))
    count, labels = connected_components(graph, directed=False)
    prior_distance=None
    if point_anchor is not None:
        prior_distance,index=cKDTree(samples).query(np.asarray(point_anchor))
        if prior_distance>.25:raise ValueError('No masked depth component is connected to the tapped 3D point (within 25 cm)')
        winner=labels[index]
    else:
        seed_labels = labels[cKDTree(samples).query(seed_points)[1]]
        winner = np.argmax(np.bincount(seed_labels, minlength=count))
    retained = samples[labels == winner]
    distances = cKDTree(retained).query(points)[0]
    points = points[distances < .012]
    footprint_points=points
    if footprint_quantile:
        # Sparse SAM/depth leaks can rotate a minimum-area rectangle even
        # when the final box uses quantiles. Trim its horizontal evidence
        # before choosing yaw, as well as before choosing width/depth.
        horizontal=(points@seed_basis)[:,[0,2]]
        limits=np.quantile(horizontal,[footprint_quantile,1-footprint_quantile],axis=0)
        footprint_points=points[np.all((horizontal>=limits[0])&(horizontal<=limits[1]),axis=1)]
    basis = footprint_frame(footprint_points, capture.manifest.anchor_to_world)
    aligned = points @ basis
    low, high = np.quantile(aligned, [.001, .999], axis=0)
    if footprint_quantile:
        limits=np.quantile(aligned[:,[0,2]],[footprint_quantile,1-footprint_quantile],axis=0)
        low[[0,2]],high[[0,2]]=limits
    extent = RefinedExtent(basis, low - margin, high + margin, planes,exclusions)
    seed_box = capture.manifest.seed.bbox_anchor
    outside_seed = ~np.all((points >= seed_box.min) & (points <= seed_box.max), axis=1)
    outside_refined = ~extent.contains(points)
    report = {'source': 'multi-view SAM-masked depth' if not geometric_fallback else 'explicit seed-connected depth fallback',
              'basis_to_anchor': basis.tolist(), 'min_local': extent.low.tolist(), 'max_local': extent.high.tolist(),
              'bbox_anchor': {'min': extent.bounds.min.tolist(), 'max': extent.bounds.max.tolist()},
              'support_planes': planes, 'support_rejection_distance_m': PLANE_DISTANCE,
              'minimum_support_area_m2': minimum_area,
              'robust_quantiles': [.001, .999], 'margin_m': margin, 'retained_points': len(points),
              'outside_seed_fraction': float(outside_seed.mean()), 'outside_refined_fraction': float(outside_refined.mean()),
              'seed_was_truncating': bool(outside_seed.mean() > .01)}
    if exclusions:
        report['floor_silhouette_consensus']=visibility
        report['floor_visibility_exclusions']={str(index):{'shape':list(mask.shape),'bits_b64':base64.b64encode(np.packbits(mask).tobytes()).decode()} for index,mask in exclusions.items()}
    if point_anchor is not None:
        report.update(point_anchor=list(point_anchor),point_anchor_distance_m=float(prior_distance))
    if footprint_quantile:
        report['footprint_robust_quantiles']=[footprint_quantile,1-footprint_quantile]
    return extent, report
