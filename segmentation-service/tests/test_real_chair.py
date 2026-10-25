import json
import base64
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from housing_segmentation.captures.extent import RefinedExtent,refine_extent,merge_support_planes
from housing_segmentation.captures.format import Capture,extract_archive,parse_manifest
from housing_segmentation.captures.geometry import depth_bbox_mask,gravity_frame
from housing_segmentation.captures.fusion import fuse_depth,simplify,reject_floor_fragments
from housing_segmentation.captures.quality import geometry_completeness


def test_noisy_floor_strips_merge_without_merging_a_table():
    rng=np.random.default_rng(5)
    floor=np.column_stack((rng.uniform(-1.5,1.5,8000),rng.normal(-.42,.018,8000),rng.uniform(-1.5,1.5,8000)))
    table=np.column_stack((rng.uniform(-.5,.5,1000),rng.normal(.3,.002,1000),rng.uniform(-.5,.5,1000)))
    planes=[{'normal_anchor':[0.,1.,0.],'offset':float(h),'observed_samples':800,'support_area_m2':7.} for h in np.linspace(.36,.45,10)]
    planes.append({'normal_anchor':[0.,1.,0.],'offset':-.3,'observed_samples':1000,'support_area_m2':1.})
    merged=merge_support_planes(planes,np.concatenate((floor,table)),[0,1,0])
    assert len(merged)==2
    f=next(p for p in merged if p.get('kind')=='floor')
    assert f['merged_plane_count']==10 and abs(f['offset']-.42)<.003
    assert .035<f['rejection_distance_m']<.06
    extent=RefinedExtent(np.eye(3),np.zeros(3),np.ones(3),merged)
    assert extent.on_support(floor).mean()>.98
    assert not extent.on_support(np.array([[0.,-.25,0.]])).any()


@pytest.mark.parametrize('axis',[0,1,2])
def test_mesh_large_shortfall_cannot_be_good_quality(axis):
    extent=RefinedExtent(np.eye(3),np.zeros(3),np.array([.6,.8,.5]),[])
    high=extent.high.copy();high[axis]*=.5
    points=np.array([[x,y,z] for x in (0,high[0]) for y in (0,high[1]) for z in (0,high[2])])
    quality=geometry_completeness(points,extent)
    assert quality['low_quality'] and quality['warnings']


def test_complete_mesh_with_small_tsdf_margin_is_not_flagged():
    extent=RefinedExtent(np.eye(3),np.zeros(3),np.array([.4,.3,.3]),[])
    points=np.array([[x,y,z] for x in (.004,.396) for y in (.004,.296) for z in (.004,.296)])
    assert not geometry_completeness(points,extent)['low_quality']


def test_complete_bounds_do_not_hide_extreme_fragmentation():
    extent=RefinedExtent(np.eye(3),np.zeros(3),np.ones(3),[])
    points=np.array([[x,y,z] for x in (0,1) for y in (0,1) for z in (0,1)])
    quality=geometry_completeness(points,extent,{'retained':2641})
    assert quality['low_quality'] and quality['fragmented_geometry']


def test_floor_visibility_ignores_occluding_views_and_rejects_background(monkeypatch):
    from housing_segmentation.captures import visibility
    # Two visible masks support the leg. The third image sees a nearer
    # surface; it must not vote against a hidden leg behind that surface.
    grid=np.array([[[16.,-.3,16.],[2.,-.4,2.]],[[16.,-.3,16.],[2.,-.4,2.]]])
    frames=[SimpleNamespace(index=i,width=32,height=32,intrinsics=SimpleNamespace(fx=100.),depth=SimpleNamespace(to_image=[1,1,0,0],width=32,height=32)) for i in range(3)]
    masks=[]
    for i in range(3):
        mask=np.zeros((32,32),bool)
        if i!=1:mask[12:20,12:20]=True
        masks.append(mask)
    def depth(frame):
        result=np.full((32,32),3000)
        if frame.index==1:result[16,16]=1000
        return result
    capture=SimpleNamespace(manifest=SimpleNamespace(frames=frames),depth=depth)
    monkeypatch.setattr(visibility,'project',lambda points,frame:(points[:,[0,2]],np.full(len(points),2.)))
    observations=[(grid,np.ones((2,2),bool),np.ones((2,2),bool)) for _ in frames]
    plane={'normal_anchor':[0,1,0],'offset':.4,'rejection_distance_m':.05}
    bad,report=visibility.floor_exclusions(capture,observations,masks,plane,lambda:None)
    assert all(np.array_equal(mask,[[False,True],[False,True]]) for mask in bad.values())
    assert report['rejected_points']==6


def test_floor_visibility_survives_refinement_report_roundtrip():
    mask=np.array([[True,False],[False,True]])
    report={'basis_to_anchor':np.eye(3).tolist(),'min_local':[0,0,0],'max_local':[1,1,1],'support_planes':[],
        'floor_visibility_exclusions':{'7':{'shape':[2,2],'bits_b64':base64.b64encode(np.packbits(mask)).decode()}}}
    restored=RefinedExtent.from_report(report)
    assert np.array_equal(restored.on_support(np.full((2,2,3),.2),frame_index=7),mask)


@pytest.fixture(scope='module')
def chair(tmp_path_factory):
    fixture=Path(__file__).resolve().parents[2]/'fixtures/real_chair_capture_v1'
    root=tmp_path_factory.mktemp('real-chair')
    payload=json.loads((fixture/'manifest.json').read_text());extract_archive(fixture/'archive.zip',root,parse_manifest(payload))
    (root/'manifest.json').write_text(json.dumps(payload))
    capture=Capture.load(root)
    masks=[np.array(Image.open(fixture/'sam_masks'/f'{f.index:04d}.png'))>0 for f in capture.manifest.frames]
    negatives=np.array(json.loads((fixture/'floor_debris_samples.json').read_text())['points_anchor_m'],np.float32)
    return capture,masks,json.loads((fixture/'provenance.json').read_text()),negatives


def test_real_chair_keeps_backrest_lower_legs_and_excludes_floor(chair,tmp_path):
    capture,raw,old,negatives=chair
    extent,report=refine_extent(capture,raw,lambda:None)
    assert len(extent.planes)==1 and extent.planes[0]['kind']=='floor'
    assert extent.planes[0]['merged_plane_count']>=5
    assert len(extent.planes[0]['frame_observations'])>=40
    assert report['floor_silhouette_consensus']['rejected_points']>100000
    masks=[]
    for f,mask in zip(capture.manifest.frames,raw):
        geometric,valid=depth_bbox_mask(capture,f,extent=extent)
        result=mask&(~valid|geometric)
        if f.index!=capture.manifest.seed.frame_index:result|=geometric
        masks.append(result)
    mesh,fusion,_=fuse_depth(capture,masks,lambda:None,object_only=True,extent=extent,diagnostic_height=0.,silhouette_masks=raw)
    vertices=np.asarray(mesh.vertices);up=gravity_frame(capture.manifest.anchor_to_world)[:,1];heights=vertices@up
    assert (heights>.05).sum()>15000
    assert heights.max()>.30 and heights.min()<-.38
    assert not geometry_completeness(vertices,extent)['low_quality']
    assert geometry_completeness(vertices,extent,fusion['component_validation'])['low_quality']
    mesh.compute_triangle_normals();centres=vertices[np.asarray(mesh.triangles)].mean(1)
    floor=extent.planes[0];normal=np.array(floor['normal_anchor'])
    fragments=((centres@normal+floor['offset'])<.015)&(np.abs(np.asarray(mesh.triangle_normals)@normal)>np.cos(np.deg2rad(35)))
    assert not fragments.any()
    simplified=simplify(mesh)
    reject_floor_fragments(simplified,extent,capture=capture,silhouette_masks=raw)
    final=np.asarray(simplified.vertices)@up
    assert len(simplified.triangles)<=50000
    assert final.max()>=heights.max()-.005
    assert (final>.05).sum()>3000
    assert final.min()<-.38
    # Fixed negative samples came from the old attached floor blanket, with
    # >=90% background evidence in >=10 clearly unoccluded camera views.
    # This catches tilted floor debris that a normal-only test misses.
    import open3d as o3d
    scene=o3d.t.geometry.RaycastingScene(nthreads=1)
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(simplified))
    floor_distances=scene.compute_distance(o3d.core.Tensor(negatives)).numpy()
    assert floor_distances.min()>.02
    assert not fusion['component_validation']['largest_only']
    assert fusion['geometry_evidence']['stages']['retained_surface_vertices']['above_reference_plus_5cm']>15000
    previous=RefinedExtent.from_report(old['previous_extent_refinement'])
    dims=np.array(old['previous_dimensions_m']);points=np.array([[x,y,z] for x in (0,dims[0]) for y in (0,dims[1]) for z in (0,dims[2])])@previous.basis.T
    assert geometry_completeness(points,previous)['low_quality']
    (tmp_path/'result.json').write_text(json.dumps({'refinement':report,'fusion':fusion,'quality':geometry_completeness(vertices,extent)},indent=2))
