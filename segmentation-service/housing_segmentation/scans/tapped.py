"""Explicit tap-seeded SAM video tracking. No automatic object discovery."""
from collections.abc import Sequence
import gc
import json
import os
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image

from ..captures.format import Capture
from ..captures.geometry import anchor_depth_points,depth_to_image,depth_bbox_mask
from ..captures.extent import refine_extent
from .discovery import Candidate
from .masking import angular_coverage


class SurfaceVotes:
    """Cross-view label consistency for the explicit targets, not discovery."""
    def __init__(self,keys,owners,voxel=.008):self.keys=keys;self.owners=owners;self.voxel=voxel
    def labels(self,points):
        from .scene import keys_for
        shape=points.shape[:-1];keys=keys_for(points.reshape(-1,3),self.voxel)
        result=np.zeros(len(keys),np.uint8)
        if len(self.keys):
            index=np.searchsorted(self.keys,keys);safe=np.minimum(index,len(self.keys)-1)
            good=(index<len(self.keys))&(self.keys[safe]==keys)
            result[good]=self.owners[safe[good]]
        return result.reshape(shape)


def surface_consensus(scan,raw,work,check):
    from .scene import keys_for
    keys=[];labels=[];weights=[]
    objects=scan.manifest.objects
    for frame_index,frame in enumerate(scan.manifest.frames):
        check();depth=scan.depth(frame);confidence=scan.confidence(frame)
        points=anchor_depth_points(frame,depth);x,y=depth_to_image(frame)
        good=(depth>0)&(confidence>=64)
        for object_index,obj in enumerate(objects):
            selected=good&raw[obj.object_id][frame_index][y,x]
            if not selected.any():continue
            unique,inverse=np.unique(keys_for(points[selected],.008),return_inverse=True)
            # One weighted vote per keyframe, rather than giving a closer
            # camera's denser pixels disproportionate authority.
            value=np.bincount(inverse,weights=confidence[selected]/255)/np.bincount(inverse)
            keys.append(unique);labels.append(np.full(len(unique),object_index,np.int32));weights.append(value)
    if not keys:return SurfaceVotes(np.array([],np.int64),np.array([],np.uint8))
    unique,inverse=np.unique(np.concatenate(keys),return_inverse=True)
    votes=np.bincount(inverse*len(objects)+np.concatenate(labels),weights=np.concatenate(weights),minlength=len(unique)*len(objects)).reshape(-1,len(objects))
    owners=votes.argmax(1).astype(np.uint8)+1
    confident=votes.max(1)>=.55*votes.sum(1)
    owners[~confident]=0
    np.savez_compressed(Path(work)/'surface-votes.npz',keys=unique,owners=owners,votes=votes.astype(np.float32))
    return SurfaceVotes(unique,owners)


class MaskFiles(Sequence):
    """Load one sensor-resolution mask at a time, including 1080p phone masks."""
    def __init__(self,folder,frames):self.folder=Path(folder);self.frames=frames
    def __len__(self):return len(self.frames)
    def __getitem__(self,index):
        if isinstance(index,slice):return [self[i] for i in range(*index.indices(len(self)))]
        frame=self.frames[index]
        with Image.open(self.folder/f'{frame.index:04d}.png') as image:return np.asarray(image)>0


def seed_prompts(objects,obj,frame,size):
    points=list(obj.points)
    for other in objects:
        if other.object_id!=obj.object_id and other.seed_frame_index==obj.seed_frame_index:
            points.extend([x,y,0] for x,y,label in other.points if label==1)
    values=np.array(points,float)
    return values[:,:2]*[size[0]/frame.width,size[1]/frame.height],values[:,2].astype(np.int32)


def saved_tracks(scan,source,work,model,check):
    """Explicit offline replay of actual saved model outputs, with input checks."""
    from .format import Scan
    from ..captures.format import required_files
    source=Path(source).resolve();original=Scan.load(source/'inputs')
    def identity(manifest):
        value=manifest.model_dump(by_alias=True);value.pop('scan_id');return value
    if identity(original.manifest)!=identity(scan.manifest):raise ValueError('Saved tracking manifest does not match the scan (apart from scan_id)')
    def digest(capture):
        checksum=hashlib.sha256()
        for name in sorted(required_files(capture.manifest)):
            check();checksum.update(name.encode())
            with (capture.root/name).open('rb') as stream:
                while block:=stream.read(1024*1024):checksum.update(block)
        return checksum.hexdigest()
    checksum=digest(scan)
    if checksum!=digest(original):raise ValueError('Saved tracking input bytes differ from this scan')
    reports={}
    for obj in scan.manifest.objects:
        metadata=json.loads((source/'masks'/obj.object_id/'tracking.json').read_text())['video_tracking']
        if metadata.get('backend')!='sam2_video' or metadata.get('model')!=model:raise ValueError('Replay requires saved official SAM video outputs for the same model')
        destination=Path(work)/'raw'/obj.object_id;destination.mkdir(parents=True,exist_ok=True)
        for frame in scan.manifest.frames:
            check();path=source/'masks/raw'/obj.object_id/f'{frame.index:04d}.png'
            with Image.open(path) as image:
                if image.mode!='L' or image.size!=(frame.width,frame.height):raise ValueError('Saved tracking mask dimensions are invalid')
            shutil.copyfile(path,destination/path.name)
        reports[obj.object_id]={**metadata,'replayed_from':str(source),'input_sha256':checksum}
    return reports


def video_masks(scan,work,check,update,*,device='cpu',model='facebook/sam2.1-hiera-tiny',predictor=None):
    """Share decoded frames in batches of four targets; keep direction per seed."""
    from ..config import SERVICE_ROOT
    os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK','1')
    os.environ.setdefault('HF_HOME',str(SERVICE_ROOT/'.cache/huggingface'))
    import torch
    frames=scan.manifest.frames;objects=scan.manifest.objects
    positions={f.index:i for i,f in enumerate(frames)}
    actual=('mps' if torch.backends.mps.is_available() else 'cpu') if device=='auto' else device
    owned=predictor is None
    if predictor is None:
        from sam2.sam2_video_predictor import SAM2VideoPredictor
        if actual=='cpu':torch.set_num_threads(min(4,os.cpu_count() or 1))
        predictor=SAM2VideoPredictor.from_pretrained(model,device=actual,apply_postprocessing=False)
    predictor.non_overlap_masks=True
    attention_hook=None
    if actual=='cpu' and hasattr(predictor,'memory_attention'):
        # SAM offloads compressed memory as bfloat16. With future conditioning
        # seeds there may be no float32 object pointer to promote that memory;
        # align the attention inputs to the unmodified CPU model's parameters.
        dtype=next(predictor.memory_attention.parameters()).dtype
        def align_attention(module,args,kwargs):
            def convert(value):
                if isinstance(value,torch.Tensor) and value.is_floating_point():return value.to(dtype=dtype)
                if isinstance(value,list):return [convert(v) for v in value]
                if isinstance(value,tuple):return tuple(convert(v) for v in value)
                if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
                return value
            return convert(args),convert(kwargs)
        attention_hook=predictor.memory_attention.register_forward_pre_hook(align_attention,with_kwargs=True)
    maximum=960
    size=(frames[0].width,frames[0].height)
    scale=min(1.,maximum/max(size));size=tuple(max(1,round(v*scale)) for v in size)
    video=Path(work)/'video';video.mkdir(parents=True,exist_ok=True)
    for i,frame in enumerate(frames):
        check();path=video/f'{i:06d}.jpg'
        if not path.exists():Image.fromarray(scan.image(frame)).resize(size).save(path,quality=95)
    state=predictor.init_state(video_path=str(video),offload_video_to_cpu=True,offload_state_to_cpu=True)
    original_getter=getattr(predictor,'_get_image_feature',None)
    encoder_cache={}
    if actual=='cpu' and original_getter is not None:
        def cached_features(inference_state,frame_idx,batch_size):
            if frame_idx in encoder_cache:inference_state['cached_features']={frame_idx:encoder_cache[frame_idx]}
            output=original_getter(inference_state,frame_idx,batch_size)
            if len(encoder_cache)<64 and frame_idx not in encoder_cache:
                encoder_cache[frame_idx]=inference_state['cached_features'][frame_idx]
            return output
        predictor._get_image_feature=cached_features
    reports={}
    try:
        for start in range(0,len(objects),4):
            batch=objects[start:start+4]
            if start:predictor.reset_state(state);encoder_cache.clear()
            for number,obj in enumerate(batch):
                check();update('segmenting_objects',.10+.35*start/len(objects),f'Following thing {start+number+1} through the scan')
                folder=Path(work)/'raw'/obj.object_id;folder.mkdir(parents=True,exist_ok=True)
                position=positions[obj.seed_frame_index];frame=frames[position]
                if obj.mask:
                    with Image.open(scan.root/obj.mask) as image:seed=cv2.resize((np.asarray(image)>0).astype(np.uint8),size,interpolation=cv2.INTER_NEAREST)>0
                    predictor.add_new_mask(state,frame_idx=position,obj_id=number+1,mask=seed)
                points,labels=seed_prompts(objects,obj,frame,size)
                negatives=any(labels==0)
                if obj.mask is None or negatives:
                    predictor.add_new_points_or_box(state,frame_idx=position,obj_id=number+1,points=points,labels=labels)
                reports[obj.object_id]={'backend':'sam2_video','model':model,'device':actual,'seed_frame_index':obj.seed_frame_index,
                                        'supplied_seed_mask':obj.mask is not None,'negative_prompts':int((labels==0).sum()),
                                        'directions':['forward','reverse'],'proxy_size':list(size),'empty_frames':0}
            earliest=min(positions[obj.seed_frame_index] for obj in batch)
            latest=max(positions[obj.seed_frame_index] for obj in batch)
            total_steps=len(frames)-earliest+latest+1
            completed_steps=0
            for reverse,begin in ((False,earliest),(True,latest)):
                for i,ids,logits in predictor.propagate_in_video(state,start_frame_idx=begin,reverse=reverse):
                    check()
                    completed_steps+=1
                    if completed_steps%5==0 or completed_steps==total_steps:
                        thing=start+(completed_steps//5)%len(batch)+1
                        fraction=(start+len(batch)*completed_steps/max(1,total_steps))/len(objects)
                        update('segmenting_objects',.10+.35*fraction,f'Following thing {thing} through the scan')
                    for number,obj in enumerate(batch):
                        seed=positions[obj.seed_frame_index]
                        if (reverse and i>seed) or (not reverse and i<seed):continue
                        mask=(logits[list(ids).index(number+1),0]>0).detach().cpu().numpy().astype(np.uint8)
                        frame=frames[i];mask=cv2.resize(mask,(frame.width,frame.height),interpolation=cv2.INTER_NEAREST)
                        Image.fromarray(mask*255).save(Path(work)/'raw'/obj.object_id/f'{frame.index:04d}.png')
            for obj in batch:
                folder=Path(work)/'raw'/obj.object_id
                for frame in frames:
                    path=folder/f'{frame.index:04d}.png'
                    if not path.exists():Image.new('L',(frame.width,frame.height)).save(path)
                masks=MaskFiles(folder,frames)
                reports[obj.object_id]['empty_frames']=int(sum(mask.sum()<20 for mask in masks))
    finally:
        if original_getter is not None:predictor._get_image_feature=original_getter
        if attention_hook is not None:attention_hook.remove()
        if owned:
            del predictor,state;gc.collect()
            if actual=='mps':torch.mps.empty_cache()
    if actual=='mps' and owned and any(r['empty_frames']>len(frames)/2 for r in reports.values()):
        reports=video_masks(scan,work,check,update,device='cpu',model=model)
        for r in reports.values():r['device_fallback_reason']='Repeated empty MPS video tracks with the same official model on CPU'
    return reports


def tapped_candidates(scan,work,check,update,*,options,predictor=None):
    frames=scan.manifest.frames;objects=scan.manifest.objects
    if options.mask_backend!='sam2':raise ValueError('Scan v2 requires official SAM video; automatic/depth-cluster discovery is disabled')
    reports=saved_tracks(scan,options.tracking_source,work,options.model,check) if options.tracking_source else video_masks(scan,work,check,update,device=options.device,model=options.model,predictor=predictor)
    raw={obj.object_id:MaskFiles(Path(work)/'raw'/obj.object_id,frames) for obj in objects}
    votes=surface_consensus(scan,raw,work,check)
    consistent=Path(work)/'consistent';consistent.mkdir(exist_ok=True)
    for obj in objects:(consistent/obj.object_id).mkdir(exist_ok=True)
    for i,frame in enumerate(frames):
        check();grid=anchor_depth_points(frame,scan.depth(frame));labels=votes.labels(grid)
        from .masking import upsample_depth_mask
        for object_index,obj in enumerate(objects):
            evidence=raw[obj.object_id][i]&upsample_depth_mask(frame,labels==object_index+1)
            Image.fromarray(evidence.astype(np.uint8)*255).save(consistent/obj.object_id/f'{frame.index:04d}.png')
    evidence_masks={obj.object_id:MaskFiles(consistent/obj.object_id,frames) for obj in objects}
    # Dots are forbidden in object IDs, so this scratch directory cannot
    # collide with a valid target named "overlap_counts".
    overlap=Path(work)/'.overlap_counts';overlap.mkdir(exist_ok=True)
    for i,frame in enumerate(frames):
        check();counts=np.zeros((frame.height,frame.width),np.uint8)
        for obj in objects:counts+=raw[obj.object_id][i].astype(np.uint8)
        Image.fromarray(counts).save(overlap/f'{frame.index:04d}.png')
    candidates=[];infos=[]
    for number,obj in enumerate(objects):
        check();update('segmenting_objects',.45+.10*number/len(objects),f'Refining thing {number+1} from its tapped depth component')
        position=next(i for i,f in enumerate(frames) if f.index==obj.seed_frame_index)
        frame=frames[position];seed=raw[obj.object_id][position]
        depth=scan.depth(frame);x,y=depth_to_image(frame)
        initial=anchor_depth_points(frame,depth)[(depth>0)&seed[y,x]&(scan.confidence(frame)>=64)]
        prior=np.array(obj.point_anchor) if obj.point_anchor is not None else (np.median(initial,axis=0) if len(initial) else np.zeros(3))
        initial_bounds=SimpleNamespace(min=initial.min(0)-.01 if len(initial) else prior-.01,max=initial.max(0)+.01 if len(initial) else prior+.01)
        proxy=Capture(scan.root,SimpleNamespace(frames=frames,anchor_to_world=scan.manifest.anchor_to_world,
                         seed=SimpleNamespace(frame_index=obj.seed_frame_index,bbox_anchor=initial_bounds)))
        folder=Path(work)/obj.object_id;folder.mkdir(parents=True,exist_ok=True)
        info={'backend':'sam2_video','model':options.model,'device':reports[obj.object_id]['device'],'video_tracking':reports[obj.object_id],
              'phone_coverage':obj.phone_coverage.model_dump(),'best_frame':position,'appearance_indexes':[],
              'views':0,'coverage_deg':0.,'validated_depth_points':0,'validated_cloud':np.empty((0,3)),
              'thumbnail_mask':seed,'structural_completion':False}
        points=initial
        try:
            extent,refinement=refine_extent(proxy,evidence_masks[obj.object_id],check,point_anchor=obj.point_anchor,footprint_quantile=.005)
            info['extent_refinement']=refinement
            counts=[];evidence=[]
            for i,frame in enumerate(frames):
                check();geometric,valid=depth_bbox_mask(proxy,frame,extent=extent)
                original=raw[obj.object_id][i]
                mask=original&(~valid|geometric)
                mask|=geometric
                # Completion cannot steal a surface claimed by a different tap.
                with Image.open(overlap/f'{frame.index:04d}.png') as image:claims=np.asarray(image)
                mask&=claims<=original.astype(np.uint8)
                depth=scan.depth(frame);x,y=depth_to_image(frame)
                grid=anchor_depth_points(frame,depth)
                accepted=(depth>0)&(scan.confidence(frame)>=64)&mask[y,x]&~extent.on_support(grid,frame_index=frame.index)
                owned=votes.labels(grid)
                allowed=(owned==0)|(owned==number+1)
                mask&=upsample_depth_mask(frame,allowed)
                # Save after enforcing cross-view ownership as well as the
                # same-frame non-overlap constraints.
                Image.fromarray(mask.astype(np.uint8)*255).save(folder/f'{frame.index:04d}.png')
                accepted&=allowed
                evidence.append(grid[accepted]);counts.append(int(accepted.sum()))
            points=np.concatenate(evidence)
            views=[frames[i] for i,count in enumerate(counts) if count>=8]
            info.update(masks=MaskFiles(folder,frames),visible_counts=counts,views=len(views),validated_depth_points=len(points),validated_cloud=points,
                        coverage_deg=angular_coverage(views,prior),extent=extent,best_frame=int(np.argmax(counts)))
            info['thumbnail_mask']=info['masks'][info['best_frame']]
            bins={}
            from ..captures.format import matrix
            for i,count in enumerate(counts):
                if count<8:continue
                camera=matrix(frames[i].camera_to_anchor)[:3,3]-prior
                sector=int((np.arctan2(camera[0],camera[2])+np.pi)/(2*np.pi)*options.sam_views)%options.sam_views
                if sector not in bins or count>counts[bins[sector]]:bins[sector]=i
            info['appearance_indexes']=sorted(bins.values())
            if len(views)<3:raise ValueError(f'Only {len(views)} reliable depth views of this tap; walk around it and retry')
        except Exception as exc:
            check();info['failure_reason']=str(exc);info.setdefault('masks',raw[obj.object_id]);info.setdefault('extent',None)
        if not len(points):points=np.array([prior])
        candidates.append(Candidate(obj.object_id,points,parent_id=None,excluded_planes=info['extent'].planes if info.get('extent') is not None else []))
        infos.append(info)
        (folder/'tracking.json').write_text(json.dumps({k:v for k,v in info.items() if k not in ('masks','validated_cloud','thumbnail_mask','extent')},indent=2,allow_nan=False))
    return candidates,infos
