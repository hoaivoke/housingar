from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from housing_segmentation.app import create_app
from housing_segmentation.config import Settings
from housing_segmentation.engine import InputError
from housing_segmentation.captures.format import extract_archive,matrix,Capture
from housing_segmentation.scans.format import Scan,parse_scan
from housing_segmentation.scans.synthetic import generate_scan
from housing_segmentation.scans.pipeline import ScanOptions
from housing_segmentation.scans.tapped import MaskFiles,seed_prompts,video_masks


@pytest.fixture(scope='module')
def v2(tmp_path_factory):
    root=tmp_path_factory.mktemp('scan-v2')/'scan'
    manifest=generate_scan(root,version=2,frames=120)
    return root,manifest,root.with_suffix('.zip')


@pytest.mark.parametrize('mutate',[
    lambda m:m.pop('objects'),lambda m:m.update(objects=[]),lambda m:m.update(objects=m['objects']*8),
    lambda m:m['objects'][1].update(object_id=m['objects'][0]['object_id']),
    lambda m:m['objects'][0].update(object_id='../bad'),lambda m:m['objects'][0].update(seed_frame_index=9000),
    lambda m:m['objects'][0].update(points=[[320,10,1]]),lambda m:m['objects'][0].update(points=[[10,-1,1]]),
    lambda m:m['objects'][0].update(points=[[10,10,2]]),lambda m:m['objects'][0].update(points=[[10,10,0]]),
    lambda m:m['objects'][0].update(point_anchor=[0,0,float('nan')]),
    lambda m:m['objects'][0].update(extra='bad'),lambda m:m['objects'][0]['phone_coverage'].update(extra=1),
    lambda m:m['objects'][0]['phone_coverage'].update(views=-1),
    lambda m:m['objects'][0]['phone_coverage'].update(coverage_deg=361),
    lambda m:m['objects'][0].update(mask='../seeds.png'),lambda m:m['objects'][0].update(mask='frames/obj_01.png'),
])
def test_v2_manifest_rejects_invalid_objects(v2,mutate):
    manifest=deepcopy(v2[1]);mutate(manifest)
    with pytest.raises(InputError):parse_scan(manifest)


@pytest.mark.parametrize('kind',['missing','wrong_size','wrong_mode','non_binary','empty','unreferenced'])
def test_v2_mask_archive_validation(v2,tmp_path,kind):
    with zipfile.ZipFile(v2[2]) as archive:files={n:archive.read(n) for n in archive.namelist()}
    from io import BytesIO
    if kind=='missing':files.pop('seeds/obj_01.png')
    elif kind=='unreferenced':files['seeds/unused.png']=files['seeds/obj_01.png']
    else:
        image=Image.new('RGB' if kind=='wrong_mode' else 'L',(4,4) if kind=='wrong_size' else (320,240),127 if kind=='non_binary' else 0 if kind=='empty' else 255)
        buffer=BytesIO();image.save(buffer,format='PNG');files['seeds/obj_01.png']=buffer.getvalue()
    path=tmp_path/'bad.zip'
    with zipfile.ZipFile(path,'w') as archive:
        for name,value in files.items():archive.writestr(name,value)
    with pytest.raises(InputError):extract_archive(path,tmp_path/'inputs',parse_scan(v2[1]))


def test_v1_remains_strict_and_unchanged(v2):
    manifest=deepcopy(v2[1]);manifest['schema']='housingar.scan.v1'
    with pytest.raises(InputError):parse_scan(manifest)
    manifest.pop('objects');assert parse_scan(manifest).schema_name=='housingar.scan.v1'


def test_full_hd_phone_pixels_and_non_offset_frame_indices(v2,tmp_path):
    manifest=deepcopy(v2[1]);manifest['frames']=manifest['frames'][:30]
    for i,frame in enumerate(manifest['frames']):
        frame.update(index=100+i*3,image='frames/phone.jpg',width=1920,height=1080,
                     intrinsics={'fx':1200.,'fy':1200.,'cx':960.,'cy':540.})
        frame['depth'].update(file='depth/phone.bin',width=160,height=90,intrinsics={'fx':100.,'fy':100.,'cx':80.,'cy':45.},to_image=[12,12,0,0])
        frame['confidence']={'file':'confidence/phone.bin'}
    manifest['objects']=[dict(object_id='phone',seed_frame_index=112,points=[[960,540,1]],mask='seeds/phone.png',point_anchor=None,phone_coverage={'views':1000,'coverage_deg':359.})]
    for name in ('frames','depth','confidence','seeds'):(tmp_path/name).mkdir()
    Image.new('RGB',(1920,1080),'white').save(tmp_path/'frames/phone.jpg')
    np.full((90,160),1500,dtype='<u2').tofile(tmp_path/'depth/phone.bin')
    np.full((90,160),220,np.uint8).tofile(tmp_path/'confidence/phone.bin')
    mask=np.zeros((1080,1920),np.uint8);mask[400:600,800:1100]=255;Image.fromarray(mask).save(tmp_path/'seeds/phone.png')
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    scan=Scan.load(tmp_path)
    obj=scan.manifest.objects[0];frame=scan.manifest.frames[4]
    points,labels=seed_prompts(scan.manifest.objects,obj,frame,(960,540))
    assert np.allclose(points,[[480,270]]) and labels.tolist()==[1]
    assert scan.depth(frame).shape==(90,160)


def test_shared_video_uses_negatives_both_directions_and_disk_masks(v2,tmp_path):
    import torch
    calls=[];messages=[]
    scan=Scan.load(v2[0])
    # Non-contiguous frame indices and different seeds use list positions only
    # for SAM; persisted mask names remain the original frame indices.
    frames=scan.manifest.frames[:3]+scan.manifest.frames[30:33]+scan.manifest.frames[60:63]
    scan.manifest=scan.manifest.model_copy(update={'frames':frames})
    class Predictor:
        def init_state(self,**kwargs):
            calls.append(('init',kwargs));return {}
        def add_new_mask(self,state,**kwargs):calls.append(('mask',kwargs))
        def add_new_points_or_box(self,state,**kwargs):calls.append(('points',kwargs))
        def propagate_in_video(self,state,start_frame_idx,reverse=False):
            calls.append(('propagate',start_frame_idx,reverse))
            for i in (range(start_frame_idx,-1,-1) if reverse else range(start_frame_idx,len(frames))):
                masks=np.full((4,1,240,320),-10,np.float32)
                x=50 if reverse else 10
                masks[:,:,10:20,x:x+10]=10
                yield i,[1,2,3,4],torch.from_numpy(masks)
    report=video_masks(scan,tmp_path,lambda:None,lambda *args:messages.append(args),predictor=Predictor())
    assert ('propagate',0,False) in calls and ('propagate',6,True) in calls
    assert any(c[0]=='points' and 0 in c[1]['labels'] for c in calls)
    assert any(c[0]=='mask' for c in calls)
    assert all(any(f'thing {i}' in entry[2] for entry in messages) for i in range(1,5))
    raw=MaskFiles(tmp_path/'raw/obj_04',frames)
    assert raw[0][12,52] and not raw[0][12,12]  # before chair seed: reverse
    assert raw[-1][12,12] and not raw[-1][12,52]  # after seed: forward
    assert (tmp_path/'raw/obj_04/060.png').exists() is False
    assert (tmp_path/'raw/obj_04/0060.png').exists()
    json.dumps(report)  # No numpy scalar metadata may break worker persistence.


def test_v2_pipeline_never_discovers_and_keeps_failure_order(v2,tmp_path,monkeypatch):
    from housing_segmentation.scans import pipeline
    from housing_segmentation.scans import tapped
    from housing_segmentation.scans.discovery import Candidate
    def forbidden(*args,**kwargs):raise AssertionError('Automatic discovery is forbidden for v2')
    monkeypatch.setattr(pipeline,'discover',forbidden);monkeypatch.setattr(pipeline,'fuse_scene',forbidden)
    def tracks(scan,work,check,update,**kwargs):
        points=np.array([[0,0,0],[.2,.3,.2]])
        return [Candidate(o.object_id,points) for o in scan.manifest.objects],[
            {'best_frame':0,'thumbnail_mask':np.ones((240,320),bool),'failure_reason':'Target not observed reliably',
             'views':1,'coverage_deg':0.,'validated_depth_points':0,'extent':None} for o in scan.manifest.objects]
    monkeypatch.setattr(tapped,'tapped_candidates',tracks)
    report=pipeline.process_scan(v2[0],tmp_path,lambda *args:None,lambda:None)
    proposals=json.loads((tmp_path/'outputs/proposals.json').read_text())['proposals']
    assert [p['proposal_id'] for p in proposals]==[o['object_id'] for o in v2[1]['objects']]
    assert all(p['parent_id'] is None and p['mesh_url'] is None and p['failure_reason'] for p in proposals)
    assert report['proposal_count']==4 and report['meshed_count']==0


def test_v2_post_keeps_same_upload_job_contract(v2,tmp_path):
    from housing_segmentation.captures.jobs import atomic_json
    def runner(inputs,work,update,check,**kwargs):
        scan=Scan.load(inputs);update('segmenting_objects',.3,'Following thing 2 through the scan')
        (work/'outputs').mkdir();atomic_json(work/'outputs/report.json',{})
        atomic_json(work/'outputs/proposals.json',{'scan_id':scan.manifest.scan_id,'proposals':[]})
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=runner)) as client:
        response=client.post('/v1/scans',data={'manifest':json.dumps(v2[1])},files={'archive':('scan.zip',v2[2].read_bytes())})
        assert response.status_code==202
        assert set(response.json())=={'scan_id','job_id'}
        assert client.post('/v1/scans',data={'manifest':json.dumps(v2[1])},files={'archive':('scan.zip',v2[2].read_bytes())}).status_code==409


def test_cli_localhost_default_and_explicit_network_bind():
    from housing_segmentation.__main__ import parse_args
    assert parse_args([]).host=='127.0.0.1'
    assert parse_args(['--host','0.0.0.0']).host=='0.0.0.0'


def test_video_batches_more_than_four_targets_without_progress_regressing(v2,tmp_path):
    import torch
    scan=Scan.load(v2[0]);frames=scan.manifest.frames[:3]
    template=scan.manifest.objects[0]
    objects=[template.model_copy(update={'object_id':f'tap_{i}','mask':None}) for i in range(5)]
    scan.manifest=scan.manifest.model_copy(update={'objects':objects,'frames':frames})
    class Predictor:
        resets=0
        def init_state(self,**kwargs):return {'ids':[]}
        def reset_state(self,state):state['ids']=[];self.resets+=1
        def add_new_points_or_box(self,state,**kwargs):state['ids'].append(kwargs['obj_id'])
        def propagate_in_video(self,state,start_frame_idx,reverse=False):
            for i in (range(start_frame_idx,-1,-1) if reverse else range(start_frame_idx,len(frames))):
                yield i,state['ids'],torch.ones((len(state['ids']),1,240,320))
    predictor=Predictor();updates=[]
    reports=video_masks(scan,tmp_path,lambda:None,lambda *args:updates.append(args),predictor=predictor)
    assert list(reports)==[obj.object_id for obj in objects] and predictor.resets==1
    assert all(r['negative_prompts']==4 for r in reports.values())
    progress=[entry[1] for entry in updates]
    assert progress==sorted(progress) and max(progress)<=.45
    assert all((tmp_path/'raw'/obj.object_id/'0002.png').exists() for obj in objects)


def test_surface_consensus_rejects_cross_view_leaks_and_density_bias(tmp_path,monkeypatch):
    from housing_segmentation.scans import tapped
    # Four close-up pixels in one frame must not beat two independent views.
    a=[.1,.2,.3];b=[.4,.5,.6];c=[.7,.8,.9]
    grid=np.array([[a,a,a],[a,b,c]])
    frames=[SimpleNamespace(index=i) for i in range(3)]
    objects=[SimpleNamespace(object_id=name) for name in ('box','cylinder')]
    scan=SimpleNamespace(manifest=SimpleNamespace(frames=frames,objects=objects),
        depth=lambda f:np.ones((2,3)),confidence=lambda f:np.array([[255,255,255],[255,255,0]],np.uint8))
    monkeypatch.setattr(tapped,'anchor_depth_points',lambda frame,depth:grid)
    monkeypatch.setattr(tapped,'depth_to_image',lambda frame:np.meshgrid(np.arange(3),np.arange(2)))
    empty=np.zeros((2,3),bool)
    box=[np.array([[1,1,1],[1,0,1]],bool),np.array([[0,0,0],[0,1,0]],bool),empty]
    cylinder=[empty,np.array([[1,0,0],[0,0,1]],bool),np.array([[1,0,0],[0,1,0]],bool)]
    votes=tapped.surface_consensus(scan,{'box':box,'cylinder':cylinder},tmp_path,lambda:None)
    assert votes.labels(np.array([a,b,c,[10,10,10]])).tolist()==[2,0,0,0]
    assert (tmp_path/'surface-votes.npz').exists()


@pytest.fixture
def saved_video(v2,tmp_path):
    from housing_segmentation.captures.format import required_files
    import shutil
    scan=Scan.load(v2[0]);source=tmp_path/'source';inputs=source/'inputs';inputs.mkdir(parents=True)
    for name in required_files(scan.manifest):
        destination=inputs/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(v2[0]/name,destination)
    (inputs/'manifest.json').write_text(json.dumps(v2[1]))
    for obj in scan.manifest.objects:
        folder=source/'masks'/obj.object_id;folder.mkdir(parents=True)
        (folder/'tracking.json').write_text(json.dumps({'video_tracking':{'backend':'sam2_video','model':ScanOptions().model,'device':'cpu'}}))
        raw=source/'masks/raw'/obj.object_id;raw.mkdir(parents=True)
        for frame in scan.manifest.frames:Image.new('L',(frame.width,frame.height),255).save(raw/f'{frame.index:04d}.png')
    return scan,source


def test_offline_saved_tracks_verify_inputs_and_allow_new_scan_id(saved_video,tmp_path):
    from housing_segmentation.scans.tapped import saved_tracks
    scan,source=saved_video
    scan.manifest=scan.manifest.model_copy(update={'scan_id':'new_replay_id'})
    report=saved_tracks(scan,source,tmp_path/'replay',ScanOptions().model,lambda:None)
    assert len(report)==4 and all(len(r['input_sha256'])==64 and r['replayed_from']==str(source) for r in report.values())
    assert MaskFiles(tmp_path/'replay/raw/obj_01',scan.manifest.frames)[0].shape==(240,320)


@pytest.mark.parametrize('kind',['input_bytes','manifest','model','mask_size'])
def test_offline_saved_tracks_reject_mismatched_provenance(saved_video,tmp_path,kind):
    from housing_segmentation.scans.tapped import saved_tracks
    scan,source=saved_video
    if kind=='input_bytes':
        image=source/'inputs'/scan.manifest.frames[0].image
        image.write_bytes(image.read_bytes()+b'changed')
    elif kind=='manifest':
        path=source/'inputs/manifest.json';manifest=json.loads(path.read_text())
        manifest['objects'][0]['points'][0][0]+=1;path.write_text(json.dumps(manifest))
    elif kind=='model':
        path=source/'masks/obj_01/tracking.json';record=json.loads(path.read_text())
        record['video_tracking']['backend']='depth_cluster';path.write_text(json.dumps(record))
    else:Image.new('L',(2,2)).save(source/'masks/raw/obj_01/0000.png')
    with pytest.raises(ValueError):saved_tracks(scan,source,tmp_path/'replay',ScanOptions().model,lambda:None)


def test_tap_selects_near_component_instead_of_largest_and_protects_its_plane(monkeypatch):
    from housing_segmentation.captures import extent
    from housing_segmentation.captures.extent import refine_extent
    # Small object top under the tap and a much larger nearby unrelated patch.
    x,z=np.meshgrid(np.linspace(-.08,.08,15),np.linspace(-.06,.06,15))
    small=np.column_stack((x.ravel(),np.full(x.size,.9),z.ravel()))
    x,z=np.meshgrid(np.linspace(.5,1.2,30),np.linspace(-.4,.4,30))
    large=np.column_stack((x.ravel(),np.full(x.size,.9),z.ravel()))
    angle=.35;c,s=np.cos(angle),np.sin(angle)
    rotation=np.array([[1,0,0],[0,c,-s],[0,s,c]])
    transform=np.eye(4);transform[:3,:3]=rotation
    cloud=np.concatenate((small,large))@rotation
    grid=cloud[None,:,:];frame=SimpleNamespace(index=0)
    bounds=SimpleNamespace(min=cloud.min(0)-.01,max=cloud.max(0)+.01)
    capture=SimpleNamespace(manifest=SimpleNamespace(frames=[frame],anchor_to_world=transform.flatten(order='F').tolist(),
        seed=SimpleNamespace(frame_index=0,bbox_anchor=bounds)),
        depth=lambda f:np.ones(grid.shape[:2]),confidence=lambda f:np.full(grid.shape[:2],255))
    monkeypatch.setattr(extent,'anchor_depth_points',lambda frame,depth:grid)
    monkeypatch.setattr(extent,'depth_to_image',lambda frame:np.meshgrid(np.arange(len(cloud)),[0]))
    normal=(np.array([0,1,0])@rotation).tolist()
    monkeypatch.setattr(extent,'fit_support_planes',lambda *args,**kwargs:[
        {'normal_anchor':normal,'offset':-.9},{'normal_anchor':normal,'offset':0.}])
    prior=np.array([0,.9,0])@rotation
    refined,report=refine_extent(capture,[np.ones(grid.shape[:2],bool)],lambda:None,point_anchor=prior)
    assert refined.contains(small@rotation).all()
    assert not refined.contains(large@rotation).any()
    assert not refined.on_support(small@rotation).any()
    assert refined.on_support(np.array([[0,0,0]])).all()
    assert len(report['support_planes'])==1 and report['point_anchor_distance_m']<.01
    monkeypatch.setattr(extent,'fit_support_planes',lambda *args,**kwargs:[])
    with pytest.raises(ValueError,match='connected to the tapped 3D point'):
        refine_extent(capture,[np.ones(grid.shape[:2],bool)],lambda:None,point_anchor=[5,5,5])


@pytest.mark.parametrize('angle',[0.,.57])
def test_sparse_horizontal_leaks_do_not_rotate_or_enlarge_tapped_extent(monkeypatch,angle):
    from housing_segmentation.captures import extent
    x,z=np.meshgrid(np.linspace(-.2,.2,45),np.linspace(-.15,.15,35))
    surface=np.column_stack((x.ravel(),np.full(x.size,.9),z.ravel()))
    # Four connected outliers (less than 0.5% of observations) enlarge a hull
    # by 8 cm; they are not enough evidence to extend the selected object.
    leaks=np.array([[x,.9,0] for x in [.22,.24,.26,.28]])
    transform=np.eye(4);c,s=np.cos(angle),np.sin(angle)
    transform[:3,:3]=[[c,0,s],[0,1,0],[-s,0,c]]
    cloud=np.concatenate((surface,leaks))@transform[:3,:3]
    grid=cloud[None,:,:];frame=SimpleNamespace(index=0)
    capture=SimpleNamespace(manifest=SimpleNamespace(frames=[frame],anchor_to_world=transform.flatten(order='F').tolist(),
        seed=SimpleNamespace(frame_index=0,bbox_anchor=SimpleNamespace(min=cloud.min(0),max=cloud.max(0)))),
        depth=lambda f:np.ones(grid.shape[:2]),confidence=lambda f:np.full(grid.shape[:2],255))
    monkeypatch.setattr(extent,'anchor_depth_points',lambda frame,depth:grid)
    monkeypatch.setattr(extent,'depth_to_image',lambda frame:np.meshgrid(np.arange(len(cloud)),[0]))
    monkeypatch.setattr(extent,'fit_support_planes',lambda *args,**kwargs:[])
    masks=[np.ones(grid.shape[:2],bool)]
    raw,_=extent.refine_extent(capture,masks,lambda:None,point_anchor=[0,.9,0])
    robust,report=extent.refine_extent(capture,masks,lambda:None,point_anchor=[0,.9,0],footprint_quantile=.005)
    assert (raw.high-raw.low)[0]>.45
    assert np.allclose((robust.high-robust.low)[[0,2]],[.408,.308],atol=.004)
    assert report['footprint_robust_quantiles']==[.005,.995]
    # The vertical estimator stays unchanged so sparse furniture feet are
    # retained; the footprint guard must not raise their bottom pivot.
    assert np.isclose(robust.low[1],raw.low[1])
