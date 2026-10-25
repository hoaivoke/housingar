from dataclasses import dataclass
import json
import os
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
import numpy as np

from .format import Scan
from .scene import fuse_scene
from .discovery import discover
from .masking import clean_candidates
from ..captures.extent import RefinedExtent
from ..captures.geometry import footprint_frame,object_local_frame
from ..captures.format import column_major,Capture
from ..captures.fusion import fuse_depth,simplify,reject_floor_fragments
from ..captures.texturing import bake_texture
from ..captures.glb import write_glb
from ..captures.validation import validate_phone_glb
from ..captures.quality import geometry_completeness
from ..captures.image_geometry import phone_images,reconstruct_images,texture_cameras,adaptive_depth
from ..assets import thumbnail


@dataclass
class ScanOptions:
    mask_backend:str='sam2'
    device:str='cpu'
    model:str='facebook/sam2.1-hiera-tiny'
    sam_views:int=12
    object_capture:bool=False
    object_capture_timeout:float=900
    geometry_backend:str="auto"
    maximum_triangles:int=20000
    tracking_source:str|None=None  # explicit offline replay only; never accepted from a manifest

    @classmethod
    def from_env(cls):
        return cls(mask_backend=os.getenv('SCAN_MASK_BACKEND','sam2'),device=os.getenv('SCAN_DEVICE','cpu'),
                   model=os.getenv('SCAN_SAM2_MODEL','facebook/sam2.1-hiera-tiny'),sam_views=int(os.getenv('SCAN_SAM_VIEWS','12')),
                   object_capture=os.getenv('SCAN_OBJECT_CAPTURE','0')=='1',
                   geometry_backend=os.getenv('SCAN_GEOMETRY_BACKEND','auto'),
                   object_capture_timeout=float(os.getenv('SCAN_OBJECT_CAPTURE_TIMEOUT','900')))


def process_scan(inputs,work,update,check,*,options=None,inference_lock=None):
    options=options or ScanOptions.from_env();work=Path(work)
    for name in ('intermediate','masks','outputs'):(work/name).mkdir(parents=True,exist_ok=True)
    started=perf_counter();timings={}
    update('validating',.02,'Validating scan keyframes and coordinate mappings')
    scan=Scan.load(inputs);check()
    tapped=hasattr(scan.manifest,'objects')
    if options.geometry_backend not in ("auto","photo","depth"):raise ValueError("Unknown geometry backend")
    depth_mode=tapped and phone_images(scan) and options.geometry_backend=="depth"
    image_mode=tapped and (options.geometry_backend=="photo" or (options.geometry_backend=="auto" and phone_images(scan)))
    if tapped:
        from .tapped import tapped_candidates
        update('segmenting_objects',.08,f'Preparing {len(scan.manifest.objects)} tapped things')
        phase=perf_counter();candidates,cleaned=tapped_candidates(scan,work/'masks',check,update,options=options);timings['segmenting_objects']=perf_counter()-phase
        scene_report={'implementation':'tap-seeded depth; no automatic scene discovery'}
        discovery={'mode':'tapped','requested_objects':len(scan.manifest.objects)}
    else:
        update('fusing_scene',.05,'Fusing confidence-weighted metric scene depth')
        phase=perf_counter();points,scene_report=fuse_scene(scan,work/'intermediate',check);timings['fusing_scene']=perf_counter()-phase
        update('finding_objects',.25,'Separating room planes, furniture and supported items')
        phase=perf_counter();candidates,discovery=discover(points,scan.manifest.anchor_to_world,check);timings['finding_objects']=perf_counter()-phase
        if not candidates:raise ValueError('No measurable object candidates found; scan closer with depth and additional views')
        (work/'intermediate/discovery.json').write_text(json.dumps({'candidates':[{'proposal_id':c.proposal_id,'parent_id':c.parent_id,'points':len(c.points)} for c in candidates],**discovery},indent=2))
        update('segmenting_objects',.35,f'Cleaning {len(candidates)} object proposals with {options.mask_backend}')
        phase=perf_counter();cleaned=clean_candidates(scan,candidates,work/'masks',check,backend=options.mask_backend,device=options.device,model=options.model,sam_views=options.sam_views);timings['segmenting_objects']=perf_counter()-phase
    proposals=[];meshes=[]
    update('meshing_objects',.60,'Fusing separate metric object meshes')
    phase=perf_counter()
    for candidate,mask_info in zip(candidates,cleaned):
        check();identifier=candidate.proposal_id;folder=work/'outputs'/identifier;folder.mkdir(exist_ok=True)
        _,placement,dimensions=object_local_frame(candidate.points,scan.manifest.anchor_to_world)
        warnings=[];method='not_built';failure=None;mesh=None;details={}
        if options.mask_backend!='sam2':warnings.append('Explicit geometric depth_cluster test mode; not learned segmentation evidence')
        best=mask_info['best_frame'];thumbnail(scan.image(scan.manifest.frames[best]),mask_info['thumbnail_mask'],folder/'thumbnail.jpg')
        try:
            if depth_mode:
                from .tapped import MaskFiles
                extent=mask_info.get('extent')
                if extent is None:raise ValueError('No reliable depth extent for the requested depth fallback')
                proxy=Capture(scan.root,SimpleNamespace(frames=scan.manifest.frames,anchor_to_world=scan.manifest.anchor_to_world,seed=SimpleNamespace(bbox_anchor=extent.bounds,frame_index=next(o.seed_frame_index for o in scan.manifest.objects if o.object_id==identifier),points=next(o.points for o in scan.manifest.objects if o.object_id==identifier))))
                raw_masks=MaskFiles(work/'masks/raw'/identifier,scan.manifest.frames)
                obj=next(o for o in scan.manifest.objects if o.object_id==identifier)
                mesh,fusion=adaptive_depth(proxy,raw_masks,check,extent=extent,point_anchor=obj.point_anchor)
                method='adaptive_depth_fallback';details['fusion']=fusion
                details['geometry_completeness']=geometry_completeness(mesh.vertices,extent)
                details['geometry_completeness']['low_quality']=True
                warnings.append('Explicit low-resolution depth fallback; inspect missing thin parts')
            elif image_mode:
                from .tapped import MaskFiles
                proxy=Capture(scan.root,scan.manifest)
                raw_dir=work/'masks/raw'/identifier
                raw_masks=MaskFiles(raw_dir,scan.manifest.frames)
                obj=next(o for o in scan.manifest.objects if o.object_id==identifier)
                update('meshing_objects',.60+.14*len(proposals)/len(candidates),f'Reconstructing thing {len(proposals)+1} from its photos')
                try:
                    mesh,image_report=reconstruct_images(proxy,raw_dir,work/'intermediate'/identifier,check,
                        timeout=options.object_capture_timeout,maximum=50000,point_anchor=obj.point_anchor)
                    method=image_report['method'];details['image_reconstruction']=image_report
                    details['geometry_completeness']=image_report['geometry_completeness']
                except Exception as exc:
                    check();warnings.append(f'Image reconstruction failed; explicit coarse depth fallback: {exc}')
                    details['image_reconstruction_error']=str(exc)
                    if mask_info.get('extent') is None:raise ValueError('Images failed and there is no reliable depth extent for fallback') from exc
                    proxy.manifest=SimpleNamespace(frames=scan.manifest.frames,anchor_to_world=scan.manifest.anchor_to_world,seed=SimpleNamespace(bbox_anchor=mask_info['extent'].bounds,frame_index=obj.seed_frame_index,points=obj.points))
                    mesh,fusion=adaptive_depth(proxy,raw_masks,check,extent=mask_info['extent'],point_anchor=obj.point_anchor,maximum=50000,silhouette_masks=raw_masks)
                    method='adaptive_depth_fallback';details['fusion']=fusion
                    details['geometry_completeness']=geometry_completeness(mesh.vertices,mask_info['extent'])
                    details['geometry_completeness']['low_quality']=True
                    warnings.append('Low-resolution depth fallback needs review; photo reconstruction did not succeed')
            else:
                if mask_info.get('failure_reason'):raise ValueError(mask_info['failure_reason'])
                if options.mask_backend=='sam2' and mask_info['validated_depth_points']<20:raise ValueError('SAM could not validate enough candidate depth')
                evidence=mask_info['validated_cloud'] if options.mask_backend=='sam2' and candidate.parent_id is not None else candidate.points
                basis=footprint_frame(evidence,scan.manifest.anchor_to_world)
                lo,hi=np.quantile(evidence@basis,[.001,.999],axis=0)
                if candidate.support_height is not None:lo[1]=min(lo[1],candidate.support_height+.010)
                extent=mask_info['extent'] if tapped else RefinedExtent(basis,lo-.004,hi+.004,candidate.excluded_planes)
                manifest=SimpleNamespace(frames=scan.manifest.frames,anchor_to_world=scan.manifest.anchor_to_world,
                                         seed=SimpleNamespace(bbox_anchor=extent.bounds))
                proxy=Capture(scan.root,manifest)
                silhouette_masks=None
                if tapped and extent.floor_visibility_exclusions:
                    from .tapped import MaskFiles
                    silhouette_masks=MaskFiles(work/'masks/raw'/identifier,scan.manifest.frames)
                method='depth_fusion'
                mesh,fusion,_=fuse_depth(proxy,mask_info['masks'],check,object_only=True,extent=extent,silhouette_masks=silhouette_masks)
                details['fusion']=fusion;details['native_attempted']=options.object_capture
                if options.object_capture and options.geometry_backend!='depth':
                    from ..captures.object_capture import run_object_capture
                    try:
                        native,registration=run_object_capture(proxy,work/'masks'/identifier,work/'intermediate'/identifier,check,timeout=options.object_capture_timeout)
                        native_dims=object_local_frame(native.vertices,scan.manifest.anchor_to_world)[2]
                        tsdf_dims=object_local_frame(mesh.vertices,scan.manifest.anchor_to_world)[2]
                        if np.any(np.abs(native_dims-tsdf_dims)>.05):raise ValueError('Native dimensions disagree with metric TSDF')
                        mesh=native;method='object_capture';details['registration']=registration
                    except Exception as exc:
                        check();warnings.append(f'Optional Object Capture failed; using TSDF: {exc}')
                mesh=simplify(mesh,options.maximum_triangles)
                details['simplification_floor_triangles_removed']=reject_floor_fragments(mesh,extent,capture=proxy,silhouette_masks=silhouette_masks,check=check)
                details['geometry_completeness']=geometry_completeness(mesh.vertices,extent,fusion.get('component_validation') if method=='depth_fusion' else None)
                warnings.extend(details['geometry_completeness']['warnings'])
            _,placement,dimensions=object_local_frame(mesh.vertices,scan.manifest.anchor_to_world)
        except Exception as exc:
            check();failure=str(exc);mesh=None;warnings.append(f'Mesh failed: {failure}')
        proposal={'proposal_id':identifier,'parent_id':candidate.parent_id,'thumbnail_url':f'/v1/scans/{scan.manifest.scan_id}/proposals/{identifier}/thumbnail.jpg',
                  'mesh_url':None,'dimensions_m':dimensions.tolist(),'object_to_anchor':column_major(placement),'method':method,
                  'quality':{'views':mask_info['views'],'coverage_deg':mask_info['coverage_deg'],'observed_fraction':0.,'low_quality':True,'warnings':warnings}}
        if failure:proposal['failure_reason']=failure
        proposals.append(proposal);meshes.append((mesh,proxy if mesh is not None else None,mask_info,folder,details))
    timings['meshing_objects']=perf_counter()-phase
    update('texturing',.76,'Baking textures from visible captured views')
    phase=perf_counter()
    for proposal,(mesh,proxy,mask_info,folder,details) in zip(proposals,meshes):
        check()
        if mesh is None:
            (folder/'report.json').write_text(json.dumps({'proposal':proposal,**details},indent=2,allow_nan=False))
            continue
        try:
            indexes=list(range(len(scan.manifest.frames))) if image_mode or depth_mode else mask_info['appearance_indexes']
            appearance=Capture(scan.root,SimpleNamespace(frames=[scan.manifest.frames[i] for i in indexes],anchor_to_world=scan.manifest.anchor_to_world))
            if image_mode or depth_mode:
                from .tapped import MaskFiles
                appearance=texture_cameras(appearance,details.get('image_reconstruction',{}))
                texture_masks=MaskFiles(work/'masks/raw'/proposal['proposal_id'],appearance.manifest.frames)
            else:texture_masks=[mask_info['masks'][i] for i in indexes]
            arrays,texture_report=bake_texture(appearance,texture_masks,mesh,check,object_only=True,inpainting='opencv',device='cpu',visibility='mesh' if image_mode or depth_mode else 'depth')
            positions,normals,uv,faces,texture=arrays
            from ..captures.format import matrix
            transform=matrix(proposal['object_to_anchor']);basis=transform[:3,:3];origin=transform[:3,3]
            write_glb(folder/'object.glb',(positions-origin)@basis,normals@basis,uv,faces,texture)
            details.update(texture=texture_report,glb_validation=validate_phone_glb(folder/'object.glb',require_closed=image_mode or depth_mode))
            proposal['mesh_url']=f"/v1/scans/{scan.manifest.scan_id}/proposals/{proposal['proposal_id']}/object.glb"
            quality=proposal['quality'];quality['observed_fraction']=texture_report['observed_texel_fraction']
            if quality['views']<8:quality['warnings'].append('Fewer than eight visible keyframes')
            if quality['coverage_deg']<180:quality['warnings'].append('Angular coverage below 180 degrees; scan additional sides')
            if quality['observed_fraction']<.6:quality['warnings'].append('Over 40% of surface texels were unobserved')
            quality['low_quality']=quality['views']<8 or quality['coverage_deg']<180 or quality['observed_fraction']<.6 or details['geometry_completeness']['low_quality']
        except Exception as exc:
            check();proposal['failure_reason']=str(exc);proposal['quality']['warnings'].append(f'Texture/export failed: {exc}')
        (folder/'report.json').write_text(json.dumps({'proposal':proposal,'segmentation':{k:v for k,v in mask_info.items() if k not in ('masks','visible_counts','validated_cloud','thumbnail_mask','extent')},**details},indent=2,allow_nan=False))
    timings['texturing']=perf_counter()-phase
    update('exporting',.97,'Saving proposals for confirmation in My stuff')
    result={'scan_id':scan.manifest.scan_id,'proposals':proposals}
    (work/'outputs/proposals.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    report={'schema':'housingar.scan.report.v2' if tapped else 'housingar.scan.report.v1','scan_id':scan.manifest.scan_id,'frames_received':len(scan.manifest.frames),
            'scene':scene_report,'discovery':discovery,'proposal_count':len(proposals),'meshed_count':sum(p['mesh_url'] is not None for p in proposals),
            'mask_backend':options.mask_backend,'timings_seconds':timings,'total_seconds':perf_counter()-started,
            'warnings':['Review source-view comparisons for unobserved or thin object parts' if tapped else 'Synthetic/real scan quality must be assessed separately; discovery is geometric and requires user confirmation']}
    (work/'outputs/report.json').write_text(json.dumps(report,indent=2,allow_nan=False));check()
    return report
