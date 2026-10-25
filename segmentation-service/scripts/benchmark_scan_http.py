#!/usr/bin/env python3
"""Upload a scan, poll/download proposals, optionally assess and confirm test assets."""
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter,sleep
from uuid import uuid4

import httpx
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from housing_segmentation.scans.assessment import assess_scan
from housing_segmentation.captures.format import matrix,column_major
from housing_segmentation.captures.validation import validate_phone_glb


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scan-id')
    parser.add_argument('--truth',type=Path)
    parser.add_argument('--confirm-library',action='store_true',help='Explicitly create synthetic demo library copies and test a layout')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    manifest=json.loads((args.folder/'manifest.json').read_text())
    manifest['scan_id']=args.scan_id or 'http_scan_'+uuid4().hex[:12]
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    measurement={'scan_id':manifest['scan_id'],'states':[],'health_seconds':[]}
    with httpx.Client(base_url=args.base_url,timeout=120) as client:
        started=perf_counter()
        with args.folder.with_suffix('.zip').open('rb') as archive:
            response=client.post('/v1/scans',data={'manifest':json.dumps(manifest)},files={'archive':('scan.zip',archive,'application/zip')})
        response.raise_for_status();assert response.status_code==202
        measurement.update(response.json());measurement['upload_seconds']=perf_counter()-started
        print(json.dumps({'accepted':response.json(),'upload_seconds':measurement['upload_seconds']}),flush=True)
        processing=perf_counter();previous=None
        while perf_counter()-processing<1800:
            response=client.get('/v1/jobs/'+measurement['job_id']);response.raise_for_status();job=response.json()
            if job['state']!=previous:
                previous=job['state'];measurement['states'].append({'state':previous,'elapsed_seconds':perf_counter()-processing})
                print(json.dumps({'state':previous,'message':job['message']}),flush=True)
            health_start=perf_counter();client.get('/health').raise_for_status();measurement['health_seconds'].append(perf_counter()-health_start)
            if job['state'] in ('done','failed','cancelled'):break
            sleep(2)
        assert job['state']=='done',job
        measurement['poll_to_done_seconds']=perf_counter()-processing
        response=client.get(job['outputs']['proposals']);response.raise_for_status();result=response.json()
        (args.output/'proposals.json').write_text(json.dumps(result,indent=2))
        for proposal in result['proposals']:
            folder=args.output/proposal['proposal_id'];folder.mkdir()
            for key,name in (('mesh_url','object.glb'),('thumbnail_url','thumbnail.jpg')):
                if proposal[key] is None:continue
                response=client.get(proposal[key]);response.raise_for_status();(folder/name).write_bytes(response.content)
            if proposal['mesh_url']:validate_phone_glb(folder/'object.glb')
        assessment=None
        if args.truth:
            assessment=assess_scan(args.output,result['proposals'],manifest['anchor_to_world'],json.loads(args.truth.read_text()))
            measurement['assessment']=assessment
        if args.confirm_library:
            named={r['proposal_id']:r['name'] for r in assessment['objects']} if assessment else {}
            items=[]
            for proposal in result['proposals']:
                if proposal['mesh_url'] is None:continue
                response=client.post('/v1/library',json={'name':'Synthetic demo '+named.get(proposal['proposal_id'],proposal['proposal_id']),
                   'source':{'scan_id':manifest['scan_id'],'proposal_id':proposal['proposal_id']}})
                response.raise_for_status();item=response.json();items.append(item)
                client.get(item['mesh_url']).raise_for_status();client.get(item['thumbnail_url']).raise_for_status()
            measurement['library_items']=items
            if items:
                duplicate=client.post('/v1/library',json={'name':'Temporary CRUD check','source':items[0]['source']});duplicate.raise_for_status()
                test_id=duplicate.json()['item_id']
                client.patch('/v1/library/'+test_id,json={'name':'Renamed CRUD check'}).raise_for_status()
                client.delete('/v1/library/'+test_id).raise_for_status()
                assert client.get(duplicate.json()['mesh_url']).status_code==404
            layout={'layout_id':manifest['scan_id'],'session_id':manifest['session_id'],'name':'Synthetic demo layout','placements':[]}
            for item in items:
                proposal=next(p for p in result['proposals'] if p['proposal_id']==item['source']['proposal_id'])
                layout['placements'].append({'item_id':item['item_id'],'pose_world':column_major(matrix(manifest['anchor_to_world'])@matrix(proposal['object_to_anchor']))})
            response=client.post('/v1/layouts',json=layout);response.raise_for_status()
            assert any(x['layout_id']==layout['layout_id'] for x in client.get('/v1/layouts').json()['layouts'])
            measurement['layout']=layout;measurement['library_crud_verified']=True
        measurement['maximum_health_seconds']=max(measurement['health_seconds'])
        (args.output/'benchmark.json').write_text(json.dumps(measurement,indent=2)+'\n')
        print(json.dumps({'scan_id':measurement['scan_id'],'done_seconds':measurement['poll_to_done_seconds'],'maximum_health_seconds':measurement['maximum_health_seconds'],
                          'assessment':assessment,'library_items':len(measurement.get('library_items',[]))}),flush=True)
        if assessment and not assessment['passes']:raise SystemExit(1)


if __name__=='__main__':main()
