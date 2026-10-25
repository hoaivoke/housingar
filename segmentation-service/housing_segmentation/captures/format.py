from dataclasses import dataclass
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Annotated, Literal
import zipfile

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, ValidationError

from ..engine import InputError, validate_points

SCHEMA = "housingar.capture.v1"
CONVENTIONS = "camera: +X right, +Y up, looks down -Z (ARCore/OpenGL). image: origin top-left, +y down. matrices: 4x4 column-major. 'anchor' = ARCore anchor created at selection; world +Y is up (gravity)."
MAX_ARCHIVE_BYTES = 400_000_000
MAX_UNCOMPRESSED_BYTES = 1_200_000_000
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]
Vector3 = Annotated[list[FiniteFloat], Field(min_length=3, max_length=3)]
Matrix16 = Annotated[list[FiniteFloat], Field(min_length=16, max_length=16)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Intrinsics(Record):
    fx: Annotated[FiniteFloat, Field(gt=0)]
    fy: Annotated[FiniteFloat, Field(gt=0)]
    cx: FiniteFloat
    cy: FiniteFloat


class Bounds(Record):
    min: Vector3
    max: Vector3


class Seed(Record):
    frame_index: NonnegativeInt
    points: list[list[FiniteFloat]]
    mask: Literal["seed_mask.png"]
    bbox_anchor: Bounds


class Depth(Record):
    file: str
    width: PositiveInt
    height: PositiveInt
    timestamp_ns: NonnegativeInt
    intrinsics: Intrinsics
    to_image: Annotated[list[FiniteFloat], Field(min_length=4, max_length=4)]


class Confidence(Record):
    file: str


class Frame(Record):
    index: NonnegativeInt
    timestamp_ns: NonnegativeInt
    tracking: Literal["TRACKING"]
    image: str
    width: PositiveInt
    height: PositiveInt
    intrinsics: Intrinsics
    camera_to_anchor: Matrix16
    camera_to_world: Matrix16
    depth: Depth
    confidence: Confidence | None = None
    sharpness: Annotated[FiniteFloat, Field(ge=0)]
    orbit_deg: FiniteFloat


class Manifest(Record):
    schema_name: Literal[SCHEMA] = Field(alias="schema")
    capture_id: Identifier
    session_id: Identifier
    mode: Literal["object"]
    label: str | None = Field(default=None, max_length=1000)
    units: Literal["meters"]
    conventions: str
    anchor_to_world: Matrix16
    seed: Seed
    frames: Annotated[list[Frame], Field(min_length=12, max_length=1000)]


def matrix(values):
    return np.asarray(values, dtype=np.float64).reshape(4, 4, order="F")


def column_major(transform):
    return np.asarray(transform).reshape(-1, order="F").tolist()


def safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise InputError("Unsafe ZIP path", 400)
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ":" in name or name.startswith("/"):
        raise InputError(f"Unsafe ZIP path: {name}", 400)
    if str(path) != name.rstrip("/"):
        raise InputError(f"Noncanonical ZIP path: {name}", 400)
    return path


def validate_transform(values, name):
    transform = matrix(values)
    if not np.all(np.isfinite(transform)) or not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-5):
        raise InputError(f"{name} must be a finite homogeneous column-major matrix")
    rotation = transform[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=0.01) or not np.isclose(np.linalg.det(rotation), 1, atol=0.01):
        raise InputError(f"{name} must contain a rigid proper rotation")


def parse_manifest(payload):
    try:
        manifest = Manifest.model_validate(payload)
    except ValidationError as exc:
        raise InputError(f"Capture manifest validation failed: {exc}") from exc
    if not all(token in manifest.conventions for token in ("+X", "+Y", "-Z", "column-major", "top-left", "anchor")):
        raise InputError("conventions must explicitly describe the capture.v1 axes and matrix layout")
    validate_transform(manifest.anchor_to_world, "anchor_to_world")
    if np.any(np.asarray(manifest.seed.bbox_anchor.max) <= manifest.seed.bbox_anchor.min):
        raise InputError("seed bbox max must exceed min on every axis")
    indices = [frame.index for frame in manifest.frames]
    if len(set(indices)) != len(indices) or manifest.seed.frame_index not in indices:
        raise InputError("Frame indices must be unique and include seed.frame_index")
    validate_frame_records(manifest.frames)
    seed_frame = next(frame for frame in manifest.frames if frame.index == manifest.seed.frame_index)
    validate_points(manifest.seed.points, seed_frame.width, seed_frame.height)
    return manifest


def validate_frame_records(frames):
    if len({frame.index for frame in frames}) != len(frames):
        raise InputError('Frame indices must be unique')
    for frame in frames:
        validate_transform(frame.camera_to_anchor, f"frame {frame.index} camera_to_anchor")
        validate_transform(frame.camera_to_world, f"frame {frame.index} camera_to_world")
        if max(frame.width * frame.height, frame.depth.width * frame.depth.height) > 40_000_000:
            raise InputError("Capture image exceeds 40 megapixels", 413)
        for path, prefix, suffix in ((frame.image, "frames/", ".jpg"), (frame.depth.file, "depth/", ".bin")):
            safe_path(path)
            if not path.startswith(prefix) or not path.lower().endswith(suffix):
                raise InputError(f"Invalid referenced path: {path}")
        if frame.confidence:
            safe_path(frame.confidence.file)
            if not frame.confidence.file.startswith("confidence/") or not frame.confidence.file.endswith(".bin"):
                raise InputError("Confidence file must be under confidence/ and end in .bin")
        sx, sy, tx, ty = frame.depth.to_image
        if sx <= 0 or sy <= 0 or tx < -0.01 or ty < -0.01 or sx * frame.depth.width + tx > frame.width + 0.01 or sy * frame.depth.height + ty > frame.height + 0.01:
            raise InputError(f"frame {frame.index} depth mapping must cover a valid cropped RGB region")
        expected = [sx * frame.depth.intrinsics.fx, sy * frame.depth.intrinsics.fy,
                    sx * frame.depth.intrinsics.cx + tx, sy * frame.depth.intrinsics.cy + ty]
        actual = [frame.intrinsics.fx, frame.intrinsics.fy, frame.intrinsics.cx, frame.intrinsics.cy]
        if not np.allclose(expected, actual, rtol=0.01, atol=1):
            raise InputError(f"frame {frame.index} depth/image intrinsics disagree with to_image")


def required_files(manifest):
    names = {manifest.seed.mask} if hasattr(manifest, 'seed') else set()
    for obj in getattr(manifest,'objects',[]):
        if obj.mask is not None:names.add(obj.mask)
    for frame in manifest.frames:
        names.update((frame.image, frame.depth.file))
        if frame.confidence:
            names.add(frame.confidence.file)
    return names


def validate_folder(root: Path, manifest: Manifest):
    root = root.resolve()
    for name in required_files(manifest):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise InputError(f"Missing or unsafe capture file: {name}")
    for frame in manifest.frames:
        try:
            with Image.open(root / frame.image) as image:
                if image.format != "JPEG" or image.size != (frame.width, frame.height):
                    raise InputError(f"RGB size/format mismatch: {frame.image}")
                image.load()
        except (OSError, Image.DecompressionBombError) as exc:
            raise InputError(f"Invalid RGB image: {frame.image}") from exc
        depth_count = frame.depth.width * frame.depth.height
        if (root / frame.depth.file).stat().st_size != depth_count * 2:
            raise InputError(f"Depth byte-size mismatch: {frame.depth.file}")
        if frame.confidence and (root / frame.confidence.file).stat().st_size != depth_count:
            raise InputError(f"Confidence byte-size mismatch: {frame.confidence.file}")
    masks=[]
    if hasattr(manifest,'seed'):
        masks.append((manifest.seed.mask,manifest.seed.frame_index))
    masks.extend((obj.mask,obj.seed_frame_index) for obj in getattr(manifest,'objects',[]) if obj.mask is not None)
    for name,index in masks:
        seed=next(frame for frame in manifest.frames if frame.index==index)
        validate_seed_mask(root/name,seed.width,seed.height)


def validate_seed_mask(path,width,height):
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode != "L" or image.size != (width,height):
                raise InputError(f"{path.name} must be an 8-bit grayscale PNG matching the seed RGB size")
            mask = np.asarray(image)
            if not np.isin(mask, [0, 255]).all() or not mask.any():
                raise InputError("Seed mask must be nonempty and contain only 0/255")
    except OSError as exc:
        raise InputError("Invalid seed mask") from exc


def extract_archive(archive: Path, destination: Path, manifest: Manifest, *, max_archive_bytes=MAX_ARCHIVE_BYTES, max_uncompressed_bytes=MAX_UNCOMPRESSED_BYTES):
    if archive.stat().st_size > max_archive_bytes:
        raise InputError(f"Archive exceeds {max_archive_bytes // 1_000_000} MB", 413)
    try:
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            if len(entries) > 4096 or sum(entry.file_size for entry in entries) > max_uncompressed_bytes:
                raise InputError("Capture uncompressed size/file count exceeds limits", 413)
            names = set()
            for entry in entries:
                safe_path(entry.filename)
                mode = entry.external_attr >> 16
                if entry.filename in names or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)) or entry.flag_bits & 1:
                    raise InputError("ZIP contains duplicate, encrypted, symlink or special entries", 400)
                names.add(entry.filename)
                if hasattr(manifest,'objects') and entry.filename.startswith('seeds/') and not entry.is_dir() and entry.filename not in required_files(manifest):
                    raise InputError('ZIP contains an unreferenced seed mask',400)
            missing = required_files(manifest) - names
            if missing:
                raise InputError(f"Capture archive is missing files: {sorted(missing)[:5]}")
            destination.mkdir(parents=True, exist_ok=True)
            for name in required_files(manifest):
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(name) as source, target.open("xb") as output:
                    import shutil
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise InputError(f"Invalid capture ZIP: {exc}", 400) from exc
    validate_folder(destination, manifest)


@dataclass
class Capture:
    root: Path
    manifest: Manifest

    @classmethod
    def load(cls, root):
        root = Path(root)
        manifest = parse_manifest(json.loads((root / "manifest.json").read_text()))
        validate_folder(root, manifest)
        return cls(root, manifest)

    def image(self, frame):
        with Image.open(self.root / frame.image) as image:
            return np.array(image.convert("RGB"))

    def depth(self, frame):
        return np.fromfile(self.root / frame.depth.file, dtype="<u2").reshape(frame.depth.height, frame.depth.width)

    def confidence(self, frame):
        if not frame.confidence:
            return np.full((frame.depth.height, frame.depth.width), 255, np.uint8)
        return np.fromfile(self.root / frame.confidence.file, dtype=np.uint8).reshape(frame.depth.height, frame.depth.width)
