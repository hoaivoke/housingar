"""Independent synthetic oracle: never imported by reconstruction/discovery."""
import numpy as np
from scipy.optimize import linear_sum_assignment
from ..captures.format import matrix
from ..captures.geometry import transform_points
from ..captures.glb import read_glb,read_accessor
from ..captures.validation import validate_phone_glb


def assess_scan(outputs,proposals,anchor_to_world,truth):
    records=[]
    for proposal in proposals:
        if proposal['mesh_url'] is None:continue
        path=outputs/proposal['proposal_id']/'object.glb'
        document,binary=read_glb(path)
        transform=matrix(anchor_to_world)@matrix(proposal['object_to_anchor'])
        points=transform_points(read_accessor(document,binary,0),transform)
        records.append((proposal,points,transform,validate_phone_glb(path)))
    objects=truth['objects']
    if not records:return {'passes':False,'reason':'No meshed proposals','objects':[]}
    costs=np.array([[np.linalg.norm((np.array(obj['min_world'])+obj['max_world'])/2-(r[1].min(0)+r[1].max(0))/2) for r in records] for obj in objects])
    rows,columns=linear_sum_assignment(costs)
    matches={objects[i]['name']:records[j][0]['proposal_id'] for i,j in zip(rows,columns)}
    results=[]
    for i,j in zip(rows,columns):
        obj=objects[i];proposal,points,transform,contract=records[j]
        lo,hi=np.array(obj['min_world']),np.array(obj['max_world']);dimensions=np.array(proposal['dimensions_m'])
        expected=hi-lo;expected[[0,2]]=sorted(expected[[0,2]],reverse=True)
        errors=np.abs(dimensions-expected)
        bottom=(lo+hi)/2;bottom[1]=lo[1]
        placement=float(np.linalg.norm(transform[:3,3]-bottom))
        parent_ok=proposal['parent_id']==matches.get(obj['parent']) if obj['parent'] else proposal['parent_id'] is None
        passed=bool(errors.max()<.02 and placement<.03 and parent_ok)
        results.append({'name':obj['name'],'proposal_id':proposal['proposal_id'],'parent_id':proposal['parent_id'],'passes':passed,
                        'dimensions_m':dimensions.tolist(),'dimension_errors_m':errors.tolist(),'placement_error_m':placement,
                        'world_min':points.min(0).tolist(),'world_max':points.max(0).tolist(),'parent_correct':parent_ok,
                        'glb':contract,'quality':proposal['quality']})
    return {'passes':len(results)==len(objects) and all(r['passes'] for r in results),'objects':results,
            'truth_objects':len(objects),'meshed_proposals':len(records),'all_proposals':len(proposals)}
