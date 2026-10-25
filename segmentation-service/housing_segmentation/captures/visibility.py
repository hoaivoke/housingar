"""Multi-view silhouette checks for ambiguous depth near a noisy floor."""
import cv2
import numpy as np

from .geometry import project


def silhouette_consistency(capture,points,masks,check,*,margin=.015,minimum_fraction=.6):
    positive=np.zeros(len(points),np.uint16);seen=np.zeros(len(points),np.uint16)
    for frame,mask in zip(capture.manifest.frames,masks):
        check();pixels,z=project(points,frame);x=np.floor(pixels[:,0]).astype(int);y=np.floor(pixels[:,1]).astype(int)
        sx,sy,tx,ty=frame.depth.to_image;dx=np.floor((pixels[:,0]-tx)/sx).astype(int);dy=np.floor((pixels[:,1]-ty)/sy).astype(int)
        good=(z>.01)&(x>=0)&(x<frame.width)&(y>=0)&(y<frame.height)&(dx>=0)&(dx<frame.depth.width)&(dy>=0)&(dy<frame.depth.height)
        ids=np.flatnonzero(good);depth=capture.depth(frame);d=depth[dy[ids],dx[ids]]/1000
        # A chair surface can hide its own legs; those views are not negative
        # silhouette evidence. Missing depth cannot establish visibility.
        ids=ids[(d>0)&(z[ids]<=d+.04)]
        distance=cv2.distanceTransform((~mask).astype(np.uint8),cv2.DIST_L2,3)
        inside=distance[y[ids],x[ids]]<=np.maximum(3.,frame.intrinsics.fx*margin/z[ids])
        seen[ids]+=1;positive[ids]+=inside
    rejected=(positive<2)|(positive<minimum_fraction*np.maximum(seen,1))
    return rejected,{'tested_points':len(points),'rejected_points':int(rejected.sum()),'minimum_positive_views':2,
        'minimum_visible_fraction':minimum_fraction,'physical_silhouette_margin_m':margin,'occlusion_tolerance_m':.04}


def floor_exclusions(capture,observations,masks,plane,check,*,margin=.015,minimum_fraction=.6):
    normal=np.asarray(plane['normal_anchor']);band=plane['rejection_distance_m']
    indexes=[];parts=[]
    for grid,good,selected in observations:
        candidates=good&((grid@normal+plane['offset'])<band+.15)
        indexes.append(candidates);parts.append(grid[candidates])
    if not any(len(p) for p in parts):return {},{}
    points=np.concatenate(parts)
    rejected,report=silhouette_consistency(capture,points,masks,check,margin=margin,minimum_fraction=minimum_fraction)
    exclusions={};offset=0
    for frame,index,part in zip(capture.manifest.frames,indexes,parts):
        bad=np.zeros(index.shape,bool);bad[index]=rejected[offset:offset+len(part)];offset+=len(part)
        exclusions[frame.index]=bad
    return exclusions,report
