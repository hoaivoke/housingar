from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from threading import Event,Lock
from time import monotonic,sleep
import zipfile

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from housing_segmentation.app import create_app
from housing_segmentation.config import Settings
from housing_segmentation.engine import InputError
from housing_segmentation.captures.jobs import JobManager,atomic_json
from housing_segmentation.captures.format import extract_archive,column_major
from housing_segmentation.captures.glb import write_glb
from housing_segmentation.scans.format import parse_scan,Scan
from housing_segmentation.scans.pipeline import ScanOptions
from housing_segmentation.scans.synthetic import generate_scan
from housing_segmentation.scans.assessment import assess_scan
from housing_segmentation.scans.scene import weighted_reduce


@pytest.fixture(scope='module')
def scan_fixture(tmp_path_factory):
    root=tmp_path_factory.mktemp('multi-object')/'scan'
    manifest=generate_scan(root,frames=120)
    return root,manifest,root.with_suffix('.zip')


def upload(client,manifest,archive):
    return client.post('/v1/scans',data={'manifest':json.dumps(manifest)},files={'archive':('scan.zip',archive.read_bytes(),'application/zip')})


def wait(client,job,states,timeout=30):
    deadline=monotonic()+timeout
    while monotonic()<deadline:
        value=client.get('/v1/jobs/'+job).json()
        if value['state'] in states:return value
        sleep(.025)
    raise AssertionError(value)


def fake_glb(path):
    positions=np.array([[0,0,0],[.2,0,0],[0,.2,0]],np.float32)
    write_glb(path,positions,np.tile([0,0,1],(3,1)),np.array([[0,0],[1,0],[0,1]]),np.array([[0,1,2]],np.uint32),np.full((2048,2048,3),180,np.uint8))


def fake_scan(inputs,work,update,check,**kwargs):
    update('finding_objects',.3,'Discovering test object')
    scan=Scan.load(inputs);folder=work/'outputs/object_001';folder.mkdir(parents=True)
    fake_glb(folder/'object.glb');Image.new('RGB',(64,64),'tan').save(folder/'thumbnail.jpg')
    proposal={'proposal_id':'object_001','parent_id':None,'mesh_url':f'/v1/scans/{scan.manifest.scan_id}/proposals/object_001/object.glb',
              'thumbnail_url':f'/v1/scans/{scan.manifest.scan_id}/proposals/object_001/thumbnail.jpg','dimensions_m':[.2,.2,.1],
              'object_to_anchor':column_major(np.eye(4)),'method':'depth_fusion',
              'quality':{'views':12,'coverage_deg':300.,'observed_fraction':.8,'low_quality':False,'warnings':[]}}
    atomic_json(work/'outputs/proposals.json',{'scan_id':scan.manifest.scan_id,'proposals':[proposal]})
    atomic_json(work/'outputs/report.json',{})


@pytest.mark.parametrize('mutation',[
    lambda x:x.update(schema='housingar.scan.v2'),lambda x:x.update(scan_id='../bad'),
    lambda x:x.update(frames=x['frames'][:29]),lambda x:x.update(frames=x['frames']*9),
    lambda x:x.update(conventions='wrong'),lambda x:x['anchor_to_world'].__setitem__(0,float('nan')),
    lambda x:x['anchor_to_world'].__setitem__(5,.9),lambda x:x['frames'][0]['depth'].update(width=999),
    lambda x:x['frames'][0]['intrinsics'].update(fx=-1),lambda x:x['frames'][0].update(index=x['frames'][1]['index']),
    lambda x:x['frames'][0].update(image='../frame.jpg'),lambda x:x['frames'][0]['camera_to_anchor'].__setitem__(3,1),
])
def test_scan_validation(scan_fixture,mutation):
    manifest=deepcopy(scan_fixture[1]);mutation(manifest)
    with pytest.raises(InputError):parse_scan(manifest)


def test_scan_reuses_byte_and_zip_validation(scan_fixture,tmp_path):
    manifest=parse_scan(scan_fixture[1]);archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr('../bad',b'bad')
    with pytest.raises(InputError):extract_archive(archive,tmp_path/'bad',manifest)
    with zipfile.ZipFile(scan_fixture[2]) as z:
        entries={name:z.read(name) for name in z.namelist()}
    entries[manifest.frames[0].depth.file]=b'x'
    with zipfile.ZipFile(archive,'w') as z:
        for name,data in entries.items():z.writestr(name,data)
    with pytest.raises(InputError,match='byte-size'):extract_archive(archive,tmp_path/'mismatch',manifest)


def test_scene_confidence_weights_are_not_binary():
    keys,values,weights=weighted_reduce(np.array([3,3]),np.array([0.,1.]),np.array([.25,.75]))
    assert values[0]==.75 and weights[0]==1


def test_shared_scan_capture_queue_cancel_restart(scan_fixture,tmp_path):
    entered,release=Event(),Event();running=0;maximum=0;guard=Lock()
    def runner(inputs,work,update,check,**kwargs):
        nonlocal running,maximum
        with guard:running+=1;maximum=max(maximum,running)
        try:
            update('fusing_scene',.2,'Waiting');entered.set()
            while not release.wait(.01):check()
            fake_scan(inputs,work,update,check)
        finally:
            with guard:running-=1
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=runner)) as client:
        response=upload(client,scan_fixture[1],scan_fixture[2]);assert response.status_code==202
        first=response.json();assert entered.wait(10)
        assert client.get('/health').status_code==200
        assert client.get(f"/v1/scans/{first['scan_id']}/proposals").status_code==409
        assert upload(client,scan_fixture[1],scan_fixture[2]).status_code==409
        second=deepcopy(scan_fixture[1]);second['scan_id']='second'
        queued=upload(client,second,scan_fixture[2]).json()
        assert client.post('/v1/jobs/'+queued['job_id']+'/cancel').json()['state']=='cancelled'
        fixture=Path(__file__).resolve().parents[2]/'fixtures/phone_capture_e2e'
        capture=client.post('/v1/captures',data={'manifest':(fixture/'manifest.json').read_text()},files={'archive':('capture.zip',(fixture/'archive.zip').read_bytes())}).json()
        assert client.get('/v1/jobs/'+capture['job_id']).json()['state']=='queued'
        assert client.post('/v1/jobs/'+capture['job_id']+'/cancel').json()['state']=='cancelled'
        release.set();done=wait(client,first['job_id'],{'done','failed'});assert done['state']=='done'
        assert done['outputs']=={'proposals':f"/v1/scans/{first['scan_id']}/proposals"}
        assert maximum==1
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=fake_scan)) as client:
        assert client.get('/v1/jobs/'+first['job_id']).json()['state']=='done'
        assert client.get(done['outputs']['proposals']).status_code==200
    interrupted=tmp_path/'scans/interrupted';interrupted.mkdir()
    atomic_json(interrupted/'job.json',{'kind':'scan','scan_id':'interrupted','job_id':'lost','state':'meshing_objects','progress':.7,'outputs':{'proposals':None}})
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=fake_scan)) as client:
        assert client.get('/v1/jobs/lost').json()['state']=='failed'


def test_active_scan_cancellation(scan_fixture,tmp_path):
    entered=Event()
    def runner(inputs,work,update,check,**kwargs):
        update('segmenting_objects',.3,'Waiting');entered.set()
        while True:check();sleep(.01)
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=runner)) as client:
        job=upload(client,scan_fixture[1],scan_fixture[2]).json()['job_id'];assert entered.wait(10)
        client.post('/v1/jobs/'+job+'/cancel').raise_for_status()
        assert wait(client,job,{'cancelled'})['outputs']=={'proposals':None}


def test_scan_upload_errors(scan_fixture,tmp_path,monkeypatch):
    import housing_segmentation.scans.format as scan_format
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.post('/v1/scans',data={'manifest':'{'},files={'archive':('x.zip',b'bad')}).status_code==400
        assert client.post('/v1/scans',data={'manifest':json.dumps(scan_fixture[1])}).status_code==422
        assert client.post('/v1/scans',data={'manifest':json.dumps(scan_fixture[1])},files={'archive':('x.zip',b'bad')}).status_code==400
        monkeypatch.setattr(scan_format,'MAX_SCAN_BYTES',1024)
        assert upload(client,scan_fixture[1],scan_fixture[2]).status_code==413


def test_failed_proposal_is_visible_and_cannot_be_confirmed(scan_fixture,tmp_path):
    def runner(inputs,work,update,check,**kwargs):
        fake_scan(inputs,work,update,check)
        path=work/'outputs/proposals.json';result=json.loads(path.read_text())
        failed=deepcopy(result['proposals'][0]);failed.update(proposal_id='failed',mesh_url=None,failure_reason='Too few views')
        failed['quality'].update(low_quality=True,warnings=['Too few views'])
        failed['thumbnail_url']=failed['thumbnail_url'].replace('object_001','failed')
        (work/'outputs/failed').mkdir();Image.new('RGB',(32,32)).save(work/'outputs/failed/thumbnail.jpg')
        result['proposals'].append(failed);atomic_json(path,result)
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=runner)) as client:
        accepted=upload(client,scan_fixture[1],scan_fixture[2]).json();wait(client,accepted['job_id'],{'done'})
        result=client.get(f"/v1/scans/{accepted['scan_id']}/proposals").json()['proposals']
        assert result[-1]['mesh_url'] is None and result[-1]['failure_reason']=='Too few views'
        assert client.get(result[-1]['thumbnail_url']).status_code==200
        assert client.post('/v1/library',json={'name':'Failed','source':{'scan_id':accepted['scan_id'],'proposal_id':'failed'}}).status_code==409


def test_capture_can_be_confirmed_into_library(tmp_path):
    fixture=Path(__file__).resolve().parents[2]/'fixtures/phone_capture_e2e'
    def runner(inputs,work,update,check,**kwargs):
        (work/'outputs').mkdir();fake_glb(work/'outputs/object.glb')
        atomic_json(work/'outputs/report.json',{'object_dimensions_m':[.2,.2,.1],'method':'depth_fusion','low_quality':False})
        (work/'outputs/background.glb').write_bytes(b'compatibility')
    with TestClient(create_app(Settings(data_dir=tmp_path),capture_runner=runner)) as client:
        accepted=client.post('/v1/captures',data={'manifest':(fixture/'manifest.json').read_text()},files={'archive':('x.zip',(fixture/'archive.zip').read_bytes())}).json()
        wait(client,accepted['job_id'],{'done'})
        item=client.post('/v1/library',json={'name':'Individual capture','source':{'job_id':accepted['job_id']}})
        assert item.status_code==201,item.text
        assert item.json()['source']=={'job_id':accepted['job_id']}
        image=Image.open(BytesIO(client.get(item.json()['thumbnail_url']).content));assert max(image.size)<=512


def test_official_sam_prompt_boundary_without_download(scan_fixture,tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    import open3d as o3d
    from housing_segmentation.scans.discovery import Candidate
    from housing_segmentation.scans.masking import clean_candidates
    from housing_segmentation.captures.geometry import transform_points
    from housing_segmentation.captures.format import matrix
    scan=Scan.load(scan_fixture[0]);calls=[]
    class Predictor:
        def set_image(self,rgb):self.shape=rgb.shape[:2]
        def predict(self,**kwargs):
            calls.append(kwargs)
            return np.ones((3,*self.shape),np.float32),np.array([.9,.8,.7]),None
    class Factory:
        @staticmethod
        def from_pretrained(model,*,device):
            assert model=='facebook/sam2.1-hiera-tiny' and device=='cpu'
            return Predictor()
    monkeypatch.setitem(sys.modules,'sam2.sam2_image_predictor',SimpleNamespace(SAM2ImagePredictor=Factory))
    candidates=[]
    for index,lo in enumerate(([-.9,.76,-.2],[.68,.43,.12])):
        mesh=o3d.geometry.TriangleMesh.create_box(.3,.25,.25).translate(lo)
        points=transform_points(np.asarray(mesh.sample_points_uniformly(3000).points),np.linalg.inv(matrix(scan.manifest.anchor_to_world)))
        candidates.append(Candidate(f'unit_{index}',points))
    result=clean_candidates(scan,candidates,tmp_path,lambda:None,sam_views=2)
    assert calls and all('box' in c and c['multimask_output'] for c in calls)
    assert any(0 in c['point_labels'] and 1 in c['point_labels'] for c in calls)
    assert all(r['validated_depth_points']>0 for r in result)
    assert all(not (a&b).any() for a,b in zip(result[0]['masks'],result[1]['masks']))


def test_real_pipeline_preserves_an_unmeshable_candidate(scan_fixture,tmp_path,monkeypatch):
    from housing_segmentation.scans import pipeline
    from housing_segmentation.scans.discovery import Candidate
    points=np.array([[x,y,z] for x in (0,.2) for y in (0,.2) for z in (0,.2)])
    candidate=Candidate('unmeshable',points)
    monkeypatch.setattr(pipeline,'fuse_scene',lambda *args: (points,{}))
    monkeypatch.setattr(pipeline,'discover',lambda *args: ([candidate],{}))
    mask=np.ones((240,320),bool)
    monkeypatch.setattr(pipeline,'clean_candidates',lambda *args,**kwargs:[{'masks':[mask]*120,'thumbnail_mask':mask,'best_frame':0,'views':120,'coverage_deg':350.,'validated_depth_points':0,'appearance_indexes':[0]}])
    def fail_mesh(*args,**kwargs):raise ValueError('Insufficient reliable surface for a mesh')
    monkeypatch.setattr(pipeline,'fuse_depth',fail_mesh)
    report=pipeline.process_scan(scan_fixture[0],tmp_path,lambda *args:None,lambda:None,options=ScanOptions(mask_backend='depth_cluster'))
    result=json.loads((tmp_path/'outputs/proposals.json').read_text())['proposals'][0]
    assert report['meshed_count']==0 and result['mesh_url'] is None
    assert result['failure_reason']=='Insufficient reliable surface for a mesh'
    assert result['quality']['low_quality'] and (tmp_path/'outputs/unmeshable/report.json').is_file()


def test_library_layouts_and_persistence(scan_fixture,tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path),scan_runner=fake_scan)) as client:
        accepted=upload(client,scan_fixture[1],scan_fixture[2]).json();assert wait(client,accepted['job_id'],{'done','failed'})['state']=='done'
        proposals=client.get(f"/v1/scans/{accepted['scan_id']}/proposals").json()['proposals'];p=proposals[0]
        assert client.get(p['mesh_url']).status_code==200
        assert Image.open(BytesIO(client.get(p['thumbnail_url']).content)).size==(64,64)
        assert client.get(f"/v1/scans/{accepted['scan_id']}/proposals/object_001/secret.txt").status_code==404
        response=client.post('/v1/library',json={'name':' My desk ','source':{'scan_id':accepted['scan_id'],'proposal_id':p['proposal_id']}})
        assert response.status_code==201;item=response.json();assert item['name']=='My desk'
        item_id=item['item_id'];assert client.get(item['mesh_url']).status_code==200
        assert client.patch('/v1/library/'+item_id,json={'name':'Desk renamed'}).json()['name']=='Desk renamed'
        for name in ('',' '*3,'x'*81):assert client.patch('/v1/library/'+item_id,json={'name':name}).status_code==422
        layout={'layout_id':'room1','session_id':'visit','name':'Room','placements':[{'item_id':item_id,'pose_world':column_major(np.eye(4))}]}
        assert client.post('/v1/layouts',json=layout).status_code==200
        invalid=deepcopy(layout);invalid['placements'][0]['pose_world'][0]=2
        assert client.post('/v1/layouts',json=invalid).status_code==422
        assert client.get('/v1/library').json()['items'][0]['item_id']==item_id
    # The copy stays available even if the source mesh is gone.
    (tmp_path/'scans'/accepted['scan_id']/'outputs/object_001/object.glb').unlink()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get(item['mesh_url']).status_code==200
        assert client.get('/v1/layouts').json()['layouts']==[layout]
        assert client.get('/v1/library').json()['items'][0]['name']=='Desk renamed'
        assert client.delete('/v1/library/'+item_id).status_code==200
        assert client.get(item['mesh_url']).status_code==404
        assert client.delete('/v1/library/'+item_id).status_code==404


@pytest.fixture(scope='module')
def processed_scan(scan_fixture,tmp_path_factory):
    root=tmp_path_factory.mktemp('scan-api-pipeline')
    with TestClient(create_app(Settings(data_dir=root),scan_options=ScanOptions(mask_backend='depth_cluster'))) as client:
        accepted=upload(client,scan_fixture[1],scan_fixture[2]).json()
        job=wait(client,accepted['job_id'],{'done','failed'},timeout=360)
        assert job['state']=='done',job
        result=client.get(job['outputs']['proposals']).json()
        for p in result['proposals']:
            assert p['mesh_url'] is not None,p
            assert client.get(p['mesh_url']).status_code==200
            image=Image.open(BytesIO(client.get(p['thumbnail_url']).content));assert max(image.size)<=512
    work=root/'scans'/accepted['scan_id']
    return work,result,scan_fixture


def test_all_objects_proposed_with_metric_meshes_and_parent_relationships(processed_scan):
    work,result,fixture=processed_scan
    truth=json.loads((fixture[0]/'ground_truth.json').read_text())
    metrics=assess_scan(work/'outputs',result['proposals'],fixture[1]['anchor_to_world'],truth)
    (work/'outputs/assessment.json').write_text(json.dumps(metrics,indent=2))
    assert metrics['passes'],metrics
    assert len(metrics['objects'])==4
