"""Ground-truth multi-object walking scan. Truth is never in its upload ZIP."""
import json
from pathlib import Path
import zipfile
import numpy as np
from PIL import Image

from ..captures.format import CONVENTIONS, column_major
from ..captures.synthetic import look_at


def generate_scan(folder, *, frames=120, width=320, height=240, scan_id='synthetic_stuff', version=1):
    import open3d as o3d
    root=Path(folder); root.mkdir(parents=True,exist_ok=False)
    for name in ('frames','depth','confidence'): (root/name).mkdir()
    def box(size,lo): return o3d.geometry.TriangleMesh.create_box(*size).translate(lo)
    desk=box([1.2,.06,.75],[-1.15,.70,-.375])
    for x in (-1.15,-.01):
        for z in (-.375,.315): desk+=box([.06,.72,.06],[x,0,z])
    target_box=box([.32,.28,.25],[-.9,.76,-.2])
    cylinder=o3d.geometry.TriangleMesh.create_cylinder(.09,.24,resolution=48)
    cylinder.rotate(o3d.geometry.get_rotation_matrix_from_xyz((np.pi/2,0,0)),center=[0,0,0]).translate([-.49,.88,-.075])
    chair=box([.44,.06,.46],[.68,.43,.12])
    chair+=box([.44,.50,.06],[.68,.45,.52])
    for x in (.68,1.065):
        for z in (.12,.525): chair+=box([.055,.45,.055],[x,0,z])
    meshes=[desk,target_box,cylinder,chair,box([5,.05,5],[-2.5,-.05,-2.5]),
            box([5,2.4,.05],[-2.5,0,-2.5]),box([5,2.4,.05],[-2.5,0,2.45]),
            box([.05,2.4,5],[-2.5,0,-2.5]),box([.05,2.4,5],[2.45,0,-2.5])]
    scene=o3d.t.geometry.RaycastingScene(nthreads=1)
    for mesh in meshes:scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    angle=.37;anchor=np.eye(4);anchor[:3,:3]=[[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]];anchor[:3,3]=[.3,0,-.2]
    k={'fx':width*.7,'fy':width*.7,'cx':width/2,'cy':height/2}
    dw,dh=width//2-20,height//2-12
    dk={'fx':k['fx']/2,'fy':k['fy']/2,'cx':(k['cx']-20)/2,'cy':(k['cy']-12)/2}
    records=[]
    taps=[]
    seed_specs=[(0,True),(0,False),(frames//4,False),(frames//2,True)]
    if version==2:
        (root/'seeds').mkdir()
        for name in ('desk','box','cylinder','chair'):(root/'truth_masks'/name).mkdir(parents=True)
    palette=np.array([[145,100,60],[195,65,45],[45,160,90],[55,90,190],[130,135,140],[170,185,200]])
    for index in range(frames):
        angle=2*np.pi*index/frames
        position=np.array([2.05*np.sin(angle),1.15+.30*np.sin(2*angle),1.90*np.cos(angle)])
        pose=look_at(position,np.array([-.12,.55,0]))
        def cast(w,h,calibration):
            y,x=np.indices((h,w),dtype=np.float32)
            direction=np.stack([(x+.5-calibration['cx'])/calibration['fx'],(calibration['cy']-y-.5)/calibration['fy'],-np.ones_like(x)],-1) @ pose[:3,:3].T
            rays=np.concatenate([np.broadcast_to(position,direction.shape),direction],-1).astype(np.float32)
            hit=scene.cast_rays(o3d.core.Tensor(rays),nthreads=1)
            return hit['t_hit'].numpy(),hit['geometry_ids'].numpy(),direction
        z,ids,direction=cast(width,height,k);valid=np.isfinite(z)
        points=position+np.where(valid,z,0)[...,None]*direction
        checker=(np.floor(points[...,0]*20)+np.floor(points[...,1]*20)+np.floor(points[...,2]*20)).astype(int)%2
        rgb=palette[np.minimum(ids,5)]*(.72+.28*checker[...,None]);rgb[~valid]=[200,205,210]
        Image.fromarray(rgb.astype(np.uint8)).save(root/f'frames/{index:04d}.jpg',quality=95)
        if version==2:
            import cv2
            for object_index,name in enumerate(('desk','box','cylinder','chair')):
                mask=(ids==object_index).astype(np.uint8)*255
                Image.fromarray(mask).save(root/f'truth_masks/{name}/{index:04d}.png')
                seed_index,with_mask=seed_specs[object_index]
                if index==seed_index:
                    if not mask.any():raise ValueError(f'Synthetic tap not visible: {name}')
                    distance=cv2.distanceTransform(mask,cv2.DIST_L2,3)
                    py,px=np.unravel_index(distance.argmax(),distance.shape)
                    identifier=f'obj_{object_index+1:02d}'
                    path=f'seeds/{identifier}.png' if with_mask else None
                    if path:Image.fromarray(mask).save(root/path)
                    tapped=(np.linalg.inv(anchor)@np.r_[points[py,px],1])[:3]
                    taps.append({'object_id':identifier,'seed_frame_index':index,'points':[[int(px),int(py),1]],'mask':path,
                                 'point_anchor':tapped.tolist(),'phone_coverage':{'views':frames,'coverage_deg':350.}})
        dz,_,_=cast(dw,dh,dk)
        depth=np.where(np.isfinite(dz)&(dz>0)&(dz<8),dz*1000,0).round().astype('<u2')
        depth.tofile(root/f'depth/{index:04d}.bin')
        # Nonconstant confidence exercises real weighted scene integration.
        confidence=np.where(depth>0,220+(np.indices(depth.shape)[1]%36),0).astype(np.uint8)
        confidence.tofile(root/f'confidence/{index:04d}.bin')
        records.append({'index':index,'timestamp_ns':1_000_000_000+index*100_000_000,'tracking':'TRACKING',
                        'image':f'frames/{index:04d}.jpg','width':width,'height':height,'intrinsics':k,
                        'camera_to_anchor':column_major(np.linalg.inv(anchor)@pose),'camera_to_world':column_major(pose),
                        'depth':{'file':f'depth/{index:04d}.bin','width':dw,'height':dh,'timestamp_ns':1_000_000_000+index*100_000_000,'intrinsics':dk,'to_image':[2,2,20,12]},
                        'confidence':{'file':f'confidence/{index:04d}.bin'},'sharpness':120.,'orbit_deg':index*360/frames})
    manifest={'schema':'housingar.scan.v1','scan_id':scan_id,'session_id':'synthetic_scan','label':'Synthetic belongings walking scan',
              'units':'meters','conventions':CONVENTIONS,'anchor_to_world':column_major(anchor),'frames':records}
    if version==2:
        manifest['schema']='housingar.scan.v2';manifest['objects']=sorted(taps,key=lambda obj:obj['object_id'])
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2))
    objects=[]
    for name,lo,hi,parent in [('desk',[-1.15,0,-.375],[.05,.76,.375],None),('box',[-.9,.76,-.2],[-.58,1.04,.05],'desk'),
                             ('cylinder',[-.58,.76,-.165],[-.40,1.,.015],'desk'),('chair',[.68,0,.12],[1.12,.95,.58],None)]:
        objects.append({'name':name,'min_world':lo,'max_world':hi,'parent':parent if version==1 else None,'object_id':f'obj_{len(objects)+1:02d}'})
    (root/'ground_truth.json').write_text(json.dumps({'objects':objects,'description':'120-frame walking loop, floor/walls, desk with touching box and cylinder, separate chair'},indent=2))
    with zipfile.ZipFile(root.with_suffix('.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
        for name in ('frames','depth','confidence'):
            for path in sorted((root/name).iterdir()):archive.write(path,path.relative_to(root).as_posix())
        if version==2:
            for path in sorted((root/'seeds').iterdir()):archive.write(path,path.relative_to(root).as_posix())
    return manifest
