#!/usr/bin/env python3
"""Run scan processing offline; optionally replay verified, saved SAM tracks."""
import argparse
import json
import fcntl
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from housing_segmentation.scans.format import Scan,parse_scan
from housing_segmentation.scans.pipeline import process_scan,ScanOptions
from housing_segmentation.captures.format import required_files
from housing_segmentation.captures.jobs import atomic_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scan-id')
    parser.add_argument('--geometry-backend',choices=('auto','photo','depth'),default='auto')
    parser.add_argument('--object-capture-timeout',type=float,default=900)
    parser.add_argument('--tracking-source',type=Path,help='A previous real-SAM job folder; its manifest and input bytes must match')
    args=parser.parse_args();scan=Scan.load(args.folder)
    payload=scan.manifest.model_dump(by_alias=True)
    if args.scan_id:payload['scan_id']=args.scan_id
    parse_scan(payload)
    work=args.output.resolve()
    if work.exists():parser.error('Output exists; use a new folder')
    # Share the live queue's lock when writing into its scans root.
    lock_root=work.parent.parent/'captures' if work.parent.name=='scans' else work.parent
    lock_root.mkdir(parents=True,exist_ok=True)
    lock=(lock_root/'.worker.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:parser.error('Stop the service before writing into its scan root')
    inputs=work/'inputs';inputs.mkdir(parents=True)
    for name in required_files(scan.manifest):
        path=inputs/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(scan.root/name,path)
    (inputs/'manifest.json').write_text(json.dumps(payload,indent=2))
    job={'kind':'scan','scan_id':payload['scan_id'],'job_id':uuid4().hex,'state':'queued','progress':0.,'message':'Offline scan','error':None,'outputs':{'proposals':None}}
    def update(state,progress,message):
        job.update(state=state,progress=progress,message=message);atomic_json(work/'job.json',job)
        with (work/'events.jsonl').open('a') as output:output.write(json.dumps({'state':state,'progress':progress,'message':message})+'\n')
        print(json.dumps({'state':state,'progress':progress,'message':message}),flush=True)
    try:
        process_scan(inputs,work,update,lambda:None,options=ScanOptions(geometry_backend=args.geometry_backend,object_capture_timeout=args.object_capture_timeout,tracking_source=str(args.tracking_source.resolve()) if args.tracking_source else None))
        job['outputs']={'proposals':f"/v1/scans/{payload['scan_id']}/proposals"};update('done',1.,'Tapped object proposals ready')
    except BaseException as exc:
        job['error']=str(exc);update('failed',job['progress'],'Offline processing failed');raise
    finally:lock.close()


if __name__=='__main__':main()
