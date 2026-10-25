"""Confirmed object copies and saved layouts, independent of scan lifetimes."""
import json
from pathlib import Path
import re
import shutil
from threading import RLock
from typing import Annotated
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image
from pydantic import Field, StrictStr, ValidationError, field_validator

from .engine import InputError
from .storage import timestamp
from .captures.format import Record, Identifier, Matrix16, validate_transform
from .captures.jobs import atomic_json
from .captures.validation import validate_phone_glb


class Named(Record):
    name: StrictStr

    @field_validator('name')
    @classmethod
    def name_valid(cls,value):
        value=value.strip()
        if not 1<=len(value)<=80:raise ValueError('Name must contain 1–80 characters')
        return value


class ScanSource(Record):
    scan_id: Identifier
    proposal_id: Identifier


class CaptureSource(Record):
    job_id: Identifier


class AddItem(Named):
    source: ScanSource | CaptureSource


class Placement(Record):
    item_id: Identifier
    pose_world: Matrix16


class Layout(Named):
    layout_id: Identifier
    session_id: Identifier
    placements: Annotated[list[Placement],Field(max_length=1000)]


def validated(model,payload):
    try:return model.model_validate(payload)
    except ValidationError as exc:raise InputError(f'Invalid request: {exc}') from exc


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):raise InputError('Invalid identifier')
    return value


def thumbnail(rgb,mask,path):
    mask=np.asarray(mask,dtype=np.uint8)
    y,x=np.nonzero(mask)
    if len(x):
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        rgb=rgb.copy();cv2.drawContours(rgb,contours,-1,(35,240,120),2)
        pad=max(6,int(max(np.ptp(x),np.ptp(y))*.12))
        rgb=rgb[max(0,y.min()-pad):min(len(rgb),y.max()+pad+1),max(0,x.min()-pad):min(rgb.shape[1],x.max()+pad+1)]
    image=Image.fromarray(rgb);image.thumbnail((512,512));image.save(path,quality=90)


class Assets:
    def __init__(self,data,jobs):
        self.root=Path(data);self.jobs=jobs;self.lock=RLock()
        for name in ('library','layouts'):(self.root/name).mkdir(exist_ok=True)

    def proposals(self,scan_id):
        job=self.jobs.scan_job(safe_id(scan_id))
        if job['state']!='done':raise InputError('Scan proposals are not available until done',409)
        return json.loads((self.jobs.folder(job)/'outputs/proposals.json').read_text())

    def proposal_file(self,scan_id,proposal_id,name):
        proposals=self.proposals(scan_id)['proposals'];safe_id(proposal_id)
        if name not in ('object.glb','thumbnail.jpg'):raise InputError('Unknown proposal file',404)
        proposal=next((p for p in proposals if p['proposal_id']==proposal_id),None)
        if proposal is None:raise InputError('Unknown proposal',404)
        if name=='object.glb' and proposal['mesh_url'] is None:raise InputError('Proposal has no reconstructed mesh',409)
        path=self.root/'scans'/scan_id/'outputs'/proposal_id/name
        if not path.is_file() or path.is_symlink():raise InputError('Proposal file missing',404)
        return path

    def item(self,item_id):
        safe_id(item_id);path=self.root/'library'/item_id/'item.json'
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():raise InputError('Unknown library item',404)
        return json.loads(path.read_text())

    def list_items(self):
        with self.lock:
            return {'items':[self.item(p.parent.name) for p in sorted((self.root/'library').glob('*/item.json')) if not p.parent.name.startswith('.') and not p.parent.is_symlink()]}

    def add(self,payload):
        request=validated(AddItem,payload)
        source=request.source.model_dump()
        with self.lock:
            if isinstance(request.source,ScanSource):
                proposals=self.proposals(request.source.scan_id)['proposals']
                proposal=next((p for p in proposals if p['proposal_id']==request.source.proposal_id),None)
                if proposal is None:raise InputError('Unknown proposal',404)
                mesh=self.proposal_file(request.source.scan_id,request.source.proposal_id,'object.glb')
                image=self.proposal_file(request.source.scan_id,request.source.proposal_id,'thumbnail.jpg')
                dimensions=proposal['dimensions_m'];method=proposal['method'];low_quality=proposal['quality']['low_quality']
                capture=None
            else:
                job=self.jobs.get(request.source.job_id)
                if job.get('kind')=='scan':raise InputError('Scan sources require scan_id and proposal_id')
                mesh=self.jobs.output_file(job['job_id'],'object.glb')
                report=json.loads(self.jobs.output_file(job['job_id'],'report.json').read_text())
                dimensions=report['object_dimensions_m'];method=report['method'];low_quality=report['low_quality']
                from .captures.format import Capture
                capture=Capture.load(self.jobs.folder(job)/'inputs')
            validate_phone_glb(mesh)
            identifier=uuid4().hex;folder=self.root/'library'/identifier;temp=self.root/'library'/('.'+identifier)
            temp.mkdir()
            try:
                shutil.copyfile(mesh,temp/'object.glb')
                if capture is None:shutil.copyfile(image,temp/'thumbnail.jpg')
                else:
                    frame=next(f for f in capture.manifest.frames if f.index==capture.manifest.seed.frame_index)
                    thumbnail(capture.image(frame),np.array(Image.open(capture.root/capture.manifest.seed.mask))>0,temp/'thumbnail.jpg')
                item={'item_id':identifier,'name':request.name,'dimensions_m':dimensions,'mesh_url':f'/v1/library/{identifier}/object.glb',
                      'thumbnail_url':f'/v1/library/{identifier}/thumbnail.jpg','source':source,'method':method,'low_quality':low_quality,'created':timestamp()}
                atomic_json(temp/'item.json',item);temp.replace(folder)
            finally:
                if temp.exists():shutil.rmtree(temp)
            return item

    def rename(self,item_id,payload):
        name=validated(Named,payload).name
        with self.lock:
            item=self.item(item_id);item['name']=name
            atomic_json(self.root/'library'/item_id/'item.json',item)
            return item

    def delete(self,item_id):
        with self.lock:
            self.item(item_id)
            folder=self.root/'library'/item_id
            trash=self.root/'library'/('.deleted-'+uuid4().hex);folder.replace(trash);shutil.rmtree(trash)
            return {'status':'deleted','item_id':item_id}

    def item_file(self,item_id,name):
        self.item(item_id)
        if name not in ('object.glb','thumbnail.jpg'):raise InputError('Unknown item file',404)
        path=self.root/'library'/item_id/name
        if not path.is_file() or path.is_symlink():raise InputError('Item file missing',404)
        return path

    def save_layout(self,payload):
        layout=validated(Layout,payload)
        with self.lock:
            for placement in layout.placements:
                self.item(placement.item_id);validate_transform(placement.pose_world,'pose_world')
            result=layout.model_dump()
            atomic_json(self.root/'layouts'/f'{layout.layout_id}.json',result)
            return result

    def layouts(self):
        with self.lock:
            return {'layouts':[json.loads(p.read_text()) for p in sorted((self.root/'layouts').glob('*.json')) if not p.is_symlink()]}
