import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from io import BytesIO
import math
from threading import Lock
from time import perf_counter

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from .config import Settings


class InputError(ValueError):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class CachedFrame:
    digest: str
    prepared: object


def decode_image(content: bytes, settings: Settings):
    if not content:
        raise InputError("image is empty", 400)
    if len(content) > settings.max_image_bytes:
        raise InputError("image exceeds the 10 MiB limit", 413)
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"JPEG", "PNG"}:
                raise InputError("image must be a JPEG or PNG", 415)
            if image.width * image.height > settings.max_image_pixels:
                raise InputError("image exceeds the 8 megapixel limit", 413)
            # Preserve sensor coordinates: deliberately do not apply EXIF rotation.
            return np.array(image.convert("RGB"))
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InputError("image could not be decoded as a valid JPEG or PNG", 400) from exc


def validate_points(points, width, height):
    if not isinstance(points, list) or not 1 <= len(points) <= 128:
        raise InputError("points must be a JSON array of 1 to 128 [x,y,label] entries")
    for point in points:
        if not isinstance(point, list) or len(point) != 3:
            raise InputError("each point must be [x,y,label]")
        x, y, label = point
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point):
            raise InputError("point coordinates and labels must be numbers")
        if not all(math.isfinite(v) for v in point):
            raise InputError("points must contain finite numbers")
        if label not in (0, 1):
            raise InputError("point label must be 1 (positive) or 0 (negative)")
        if not (0 <= x < width and 0 <= y < height):
            raise InputError(f"point ({x}, {y}) is outside the {width}x{height} image")


def encode_masks(masks, scores, width, height):
    encoded = []
    for mask, score in zip(masks, scores):
        pixels = (np.asarray(mask, dtype=bool).astype(np.uint8) * 255)
        if pixels.shape != (height, width):
            raise RuntimeError("backend returned a mask with incorrect dimensions")
        if not math.isfinite(float(score)):
            raise RuntimeError("backend returned a non-finite score")
        contours, _ = cv2.findContours(pixels, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            # Polygon describes the largest component; the PNG retains all regions/holes.
            contour = max(contours, key=cv2.contourArea)
            outline = cv2.approxPolyDP(contour, 1.5, True).reshape(-1, 2).tolist()
            x, y, w, h = cv2.boundingRect(pixels)
            bbox = [x, y, x + w, y + h]
        else:
            outline, bbox = [], [0, 0, 0, 0]
        stream = BytesIO()
        Image.fromarray(pixels).save(stream, format="PNG")
        encoded.append({
            "score": float(score),
            "area_fraction": float(np.count_nonzero(pixels) / (width * height)),
            "mask_png_b64": base64.b64encode(stream.getvalue()).decode("ascii"),
            "bbox": bbox,
            "polygon": outline,
        })
    return sorted(encoded, key=lambda mask: mask["score"], reverse=True)


class SegmentationEngine:
    def __init__(self, settings: Settings, backend):
        self.settings = settings
        self.backend = backend
        self._cache = OrderedDict()
        self._lock = Lock()

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    def segment(self, content: bytes, frame_id: str, session_id: str, points):
        started = perf_counter()
        rgb = decode_image(content, self.settings)
        height, width = rgb.shape[:2]
        validate_points(points, width, height)
        digest = hashlib.sha256(content).hexdigest()
        decoded = perf_counter()
        # The lock covers embedding AND prediction: predictor state must not race.
        with self._lock:
            key = (session_id, frame_id)
            entry = self._cache.get(key)
            cached = entry is not None
            if cached and entry.digest != digest:
                raise InputError("frame_id was reused with different image bytes in this session", 409)
            embed_ms = 0.0
            if not cached:
                # Evict before embedding to bound retained per-frame GPU memory.
                while len(self._cache) >= self.settings.cache_frames:
                    self._cache.popitem(last=False)
                self.backend.synchronize()
                embed_start = perf_counter()
                prepared = self.backend.embed(rgb)
                self.backend.synchronize()
                embed_ms = (perf_counter() - embed_start) * 1000
                entry = CachedFrame(digest, prepared)
                self._cache[key] = entry
            self._cache.move_to_end(key)
            self.backend.synchronize()
            predict_start = perf_counter()
            masks, scores = self.backend.predict(entry.prepared, points)
            self.backend.synchronize()
            predict_ms = (perf_counter() - predict_start) * 1000
        post_start = perf_counter()
        encoded = encode_masks(masks, scores, width, height)
        ended = perf_counter()
        return {
            "frame_id": frame_id,
            "backend": self.backend.name,
            "model": self.backend.model,
            "width": width,
            "height": height,
            "embedding_cached": cached,
            "masks": encoded,
            "timing_ms": {
                "decode": (decoded - started) * 1000,
                "embed": embed_ms,
                "predict": predict_ms,
                "postprocess": (ended - post_start) * 1000,
                "total": (ended - started) * 1000,
            },
        }
