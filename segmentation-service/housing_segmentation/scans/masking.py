from pathlib import Path
import numpy as np
import cv2
from PIL import Image

from ..captures.geometry import anchor_depth_points, depth_to_image, project
from ..captures.format import matrix


def upsample_depth_mask(frame, selected):
    y,x=np.indices((frame.height,frame.width),dtype=np.float32)
    sx,sy,tx,ty=frame.depth.to_image
    return cv2.remap(selected.astype(np.uint8),(x+.5-tx)/sx-.5,(y+.5-ty)/sy-.5,cv2.INTER_NEAREST)>0


def angular_coverage(frames, centre):
    if len(frames)<2:return 0.
    positions=np.array([matrix(f.camera_to_anchor)[:3,3] for f in frames])-centre
    angles=np.sort(np.degrees(np.arctan2(positions[:,0],positions[:,2]))%360)
    return float(360-np.max(np.diff(np.r_[angles,angles[0]+360])))


def clean_candidates(scan,candidates,work,check,*,backend='sam2',device='cpu',model='facebook/sam2.1-hiera-tiny',sam_views=12):
    from scipy.spatial import cKDTree
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    trees=[cKDTree(c.points) for c in candidates]
    masks=[[] for _ in candidates];visible=[[] for _ in candidates]
    depth_grids=[]
    for frame in scan.manifest.frames:
        check();depth=scan.depth(frame);points=anchor_depth_points(frame,depth)
        good=(depth>0)&(scan.confidence(frame)>=64)
        flattened=points.reshape(-1,3)
        distances=np.stack([tree.query(flattened)[0] for tree in trees],1)
        owner=distances.argmin(1).reshape(depth.shape)
        for i,candidate in enumerate(candidates):
            selected=good&(owner==i)&(distances[:,i].reshape(depth.shape)<.025)
            for plane in candidate.excluded_planes:
                selected &= np.abs(points@plane['normal_anchor']+plane['offset'])>.015
            full=upsample_depth_mask(frame,selected)
            masks[i].append(full)
            visible[i].append(int(selected.sum()))
        depth_grids.append((points,good))
    selected_views=[]
    for candidate,counts in zip(candidates,visible):
        centre=np.median(candidate.points,axis=0)
        bins={}
        for i,(frame,count) in enumerate(zip(scan.manifest.frames,counts)):
            if count<8:continue
            delta=matrix(frame.camera_to_anchor)[:3,3]-centre
            sector=int((np.arctan2(delta[0],delta[2])+np.pi)/(2*np.pi)*sam_views)%sam_views
            if sector not in bins or count>counts[bins[sector]]:bins[sector]=i
        selected_views.append(set(bins.values()))
    model_masks=[{} for _ in candidates];actual_device='cpu'
    if backend=='sam2':
        import os
        from ..config import SERVICE_ROOT
        os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK','1')
        os.environ.setdefault('HF_HOME',str(SERVICE_ROOT/'.cache/huggingface'))
        import torch
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        torch.set_num_threads(4)
        actual_device=('mps' if torch.backends.mps.is_available() else 'cpu') if device=='auto' else device
        predictor=SAM2ImagePredictor.from_pretrained(model,device=actual_device)
        for frame_index,frame in enumerate(scan.manifest.frames):
            targets=[i for i,views in enumerate(selected_views) if frame_index in views]
            if not targets:continue
            check()
            with torch.inference_mode():predictor.set_image(scan.image(frame))
            for i in targets:
                geometric=masks[i][frame_index]
                y,x=np.nonzero(geometric)
                if not len(x):continue
                distance=cv2.distanceTransform(geometric.astype(np.uint8),cv2.DIST_L2,3)
                py,px=np.unravel_index(distance.argmax(),distance.shape)
                coords=[[px,py]];labels=[1]
                for other in range(len(candidates)):
                    if other==i:continue
                    oy,ox=np.nonzero(masks[other][frame_index])
                    if len(ox):coords.append([float(np.median(ox)),float(np.median(oy))]);labels.append(0)
                box=np.array([max(0,x.min()-2),max(0,y.min()-2),min(frame.width-1,x.max()+2),min(frame.height-1,y.max()+2)],np.float32)
                with torch.inference_mode():
                    predicted,scores,_=predictor.predict(point_coords=np.array(coords,np.float32),point_labels=np.array(labels),box=box,multimask_output=True)
                predicted=np.asarray(predicted)>0
                overlap=[(mask&geometric).sum()/max(1,(mask|geometric).sum()) for mask in predicted]
                model_masks[i][frame_index]=predicted[int(np.argmax(overlap))]&geometric
        del predictor
        if actual_device=='mps':torch.mps.empty_cache()
    elif backend!='depth_cluster':raise ValueError('Scan mask backend must be sam2 or explicit depth_cluster test mode')
    result=[]
    for i,candidate in enumerate(candidates):
        check();folder=work/candidate.proposal_id;folder.mkdir(exist_ok=True)
        evidence=[]
        for frame_index,mask in model_masks[i].items():
            frame=scan.manifest.frames[frame_index];x,y=depth_to_image(frame)
            points,good=depth_grids[frame_index]
            evidence.append(points[good&mask[y,x]])
            Image.fromarray(mask.astype(np.uint8)*255).save(folder/f'sam_{frame.index:04d}.png')
        evidence=np.concatenate(evidence) if evidence else np.empty((0,3))
        accepted_tree=cKDTree(evidence) if len(evidence) else None
        cleaned=[]
        for frame_index,frame in enumerate(scan.manifest.frames):
            check();mask=masks[i][frame_index].copy()
            if backend=='sam2':
                if accepted_tree is None:mask[:]=False
                elif candidate.parent_id is None:
                    # Keep the connected furniture structure (legs/back/seat)
                    # once SAM validates it; sparse prompts can select just a
                    # tabletop. Other candidates/room planes remain excluded.
                    pass
                else:
                    points,good=depth_grids[frame_index]
                    accepted=accepted_tree.query(points.reshape(-1,3))[0].reshape(good.shape)<.015
                    mask &= upsample_depth_mask(frame,accepted&good)
            cleaned.append(mask)
            Image.fromarray(mask.astype(np.uint8)*255).save(folder/f'{frame.index:04d}.png')
        views=[frame for frame,count in zip(scan.manifest.frames,visible[i]) if count>=8]
        best=int(np.argmax(visible[i]))
        result.append({'masks':cleaned,'best_frame':best,'visible_counts':visible[i],
                       'thumbnail_mask':masks[i][best],
                       'validated_cloud':evidence,
                       'structural_completion':candidate.parent_id is None and backend=='sam2',
                       'appearance_indexes':sorted(selected_views[i]),
                       'model_views':len(selected_views[i]),'validated_depth_points':len(evidence),
                       'backend':backend,'device':actual_device,'model':model if backend=='sam2' else None,
                       'views':len(views),'coverage_deg':angular_coverage(views,np.median(candidate.points,axis=0))})
    return result
