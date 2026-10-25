import json
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from pydantic import Field, ValidationError

from ..engine import InputError
from ..captures.format import Record, Identifier, Matrix16, Vector3, Frame, NonnegativeInt, CONVENTIONS, Capture, validate_transform, validate_frame_records, validate_folder, matrix, safe_path
from ..engine import validate_points

MAX_SCAN_BYTES = 600_000_000
MAX_SCAN_UNCOMPRESSED = 1_800_000_000


class ScanManifest(Record):
    schema_name: Literal['housingar.scan.v1'] = Field(alias='schema')
    scan_id: Identifier
    session_id: Identifier
    label: str | None = Field(default=None, max_length=1000)
    units: Literal['meters']
    conventions: Literal[CONVENTIONS]
    anchor_to_world: Matrix16
    frames: Annotated[list[Frame], Field(min_length=30, max_length=1000)]


class PhoneCoverage(Record):
    views: NonnegativeInt
    coverage_deg: Annotated[float,Field(ge=0,le=360,allow_inf_nan=False)]


class TappedObject(Record):
    object_id: Identifier
    seed_frame_index: NonnegativeInt
    points: Annotated[list[list[float]],Field(min_length=1)]
    mask: str | None = None
    point_anchor: Vector3 | None = None
    phone_coverage: PhoneCoverage


class ScanManifestV2(ScanManifest):
    schema_name: Literal['housingar.scan.v2'] = Field(alias='schema')
    objects: Annotated[list[TappedObject],Field(min_length=1,max_length=30)]


def parse_scan(payload):
    try:
        model=ScanManifestV2 if isinstance(payload,dict) and payload.get('schema')=='housingar.scan.v2' else ScanManifest
        manifest = model.model_validate(payload)
    except ValidationError as exc:
        raise InputError(f'Scan manifest validation failed: {exc}') from exc
    validate_transform(manifest.anchor_to_world, 'anchor_to_world')
    if not np.allclose(matrix(manifest.anchor_to_world)[:3, 1], [0, 1, 0], atol=.01):
        raise InputError('Scan anchor must be level: +Y world up')
    validate_frame_records(manifest.frames)
    if isinstance(manifest,ScanManifestV2):
        frames={frame.index:frame for frame in manifest.frames}
        ids=[obj.object_id for obj in manifest.objects]
        if len(set(ids))!=len(ids):raise InputError('Object IDs must be unique')
        for obj in manifest.objects:
            if obj.seed_frame_index not in frames:raise InputError(f'{obj.object_id}: seed frame does not exist')
            frame=frames[obj.seed_frame_index]
            validate_points(obj.points,frame.width,frame.height)
            if not any(p[2]==1 for p in obj.points):raise InputError(f'{obj.object_id}: at least one positive tap is required')
            if obj.mask is not None:
                safe_path(obj.mask)
                if obj.mask!=f'seeds/{obj.object_id}.png':raise InputError('Object mask must be seeds/<object_id>.png')
    return manifest


class Scan(Capture):
    @classmethod
    def load(cls, root):
        root = Path(root)
        manifest = parse_scan(json.loads((root / 'manifest.json').read_text()))
        validate_folder(root, manifest)
        return cls(root, manifest)
