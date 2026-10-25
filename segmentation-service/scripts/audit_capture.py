#!/usr/bin/env python3
"""Reassess a saved capture's geometry; optionally update its quality report."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from housing_segmentation.captures.extent import RefinedExtent
from housing_segmentation.captures.format import matrix
from housing_segmentation.captures.glb import read_glb,read_accessor
from housing_segmentation.captures.geometry import transform_points
from housing_segmentation.captures.quality import geometry_completeness
from housing_segmentation.captures.jobs import atomic_json


def audit(folder,write=False):
    folder=Path(folder);path=folder/'outputs/report.json';mesh=folder/'outputs/object.glb'
    report=json.loads(path.read_text());document,binary=read_glb(mesh)
    accessor=document['meshes'][0]['primitives'][0]['attributes']['POSITION']
    points=transform_points(read_accessor(document,binary,accessor),matrix(report['object_to_anchor']))
    components=report.get('object_depth_comparison',{}).get('component_validation') if report.get('method')=='depth_fusion' else None
    quality=geometry_completeness(points,RefinedExtent.from_report(report['extent_refinement']),components)
    result={'geometry':quality,'low_quality':bool(report.get('low_quality') or quality['low_quality']),
        'artifact_sha256':hashlib.sha256(mesh.read_bytes()).hexdigest(),'sam_recomputed':False,
        'audited_at':datetime.now(timezone.utc).isoformat()}
    atomic_json(folder/'outputs/geometry-audit.json',result)
    if write:
        backup=folder/'outputs/report.before_geometry_audit.json'
        if not backup.exists():shutil.copyfile(path,backup)
        report['object_geometry_completeness']=quality;report['low_quality']=result['low_quality'];report['geometry_audit']=result
        warnings=report.setdefault('warnings',[])
        for warning in quality['warnings']:
            if warning not in warnings:warnings.append(warning)
        atomic_json(path,report)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path)
    parser.add_argument('--write-report',action='store_true',help='Back up the old report, then apply the current quality guard')
    args=parser.parse_args();print(json.dumps(audit(args.folder,args.write_report),indent=2))
