#!/usr/bin/env python3
"""Prepare a new, reviewable photo folder from object photographs or orbit video."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image, ImageOps


def photos(path):
    for source in sorted(path.iterdir()):
        if source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".tif", ".tiff"} or not source.is_file():
            continue
        try:
            with Image.open(source) as image:
                rgb = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
                yield rgb, {"file": str(source.resolve())}, None
        except (OSError, ValueError) as exc:
            yield None, {"file": str(source.resolve())}, f"decode_error: {exc}"


def video(path, every_seconds, orientation):
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError("Cannot open input video")
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("Video has no usable frame rate")
        # Rotation is explicit for deterministic behavior across OpenCV builds.
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        stride = max(1, round(fps * every_seconds))
        index = 0
        while True:
            success, bgr = capture.read()
            if not success:
                break
            if index % stride == 0:
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                if orientation:
                    rgb = np.ascontiguousarray(np.rot90(rgb, -(orientation // 90)))
                yield rgb, {"file": str(path.resolve()), "video_frame": index,
                            "time_seconds_estimated_from_fps": index / fps,
                            "fps": fps, "rotation_deg_clockwise": orientation}, None
            index += 1
    finally:
        capture.release()


def prepare(args):
    source, output = args.input.resolve(), args.output.resolve()
    if not source.exists():
        raise ValueError("Input does not exist")
    if output == source or source.is_dir() and output.is_relative_to(source):
        raise ValueError("Output must be outside the source photo folder")
    if output.exists():
        raise ValueError("Output already exists; choose a new folder to preserve existing work")
    iterator = photos(source) if source.is_dir() else video(source, args.sample_every, args.rotate)
    accepted, rejected = [], []
    last_thumbnail = None
    digests = set()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1, "status": "preparing", "input": str(source),
        "input_kind": "photos" if source.is_dir() else "video",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scale_status": "unverified", "photogrammetry_attempted": False,
        "options": {key: getattr(args, key) for key in ("sample_every", "max_frames", "max_side", "min_sharpness", "min_difference", "min_frames", "rotate")},
        "accepted": accepted, "rejected": rejected,
        "warnings": ["Quality filters do not establish multi-view overlap or capture coverage.",
                     "Prepared JPEGs do not retain source depth metadata. Use original photos directly when depth metadata should be preserved."],
    }
    try:
        for rgb, provenance, error in iterator:
            if error:
                rejected.append(dict(provenance, reason=error))
                continue
            image = Image.fromarray(rgb)
            image.thumbnail((args.max_side, args.max_side), Image.Resampling.LANCZOS)
            rgb = np.asarray(image)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            digest = hashlib.sha256(rgb.tobytes()).hexdigest()
            thumbnail = cv2.resize(gray, (64, 64)).astype(np.float32)
            difference = float(np.mean(np.abs(thumbnail - last_thumbnail))) if last_thumbnail is not None else None
            reason = None
            if digest in digests:
                reason = "duplicate_pixels"
            elif sharpness < args.min_sharpness:
                reason = "low_sharpness"
            elif difference is not None and difference < args.min_difference:
                reason = "near_duplicate"
            if reason:
                rejected.append(dict(provenance, reason=reason, sharpness=sharpness, difference=difference))
                continue
            name = f"frame_{len(accepted):05d}.jpg"
            image.save(output / name, "JPEG", quality=95)
            accepted.append(dict(provenance, output_file=name, width=image.width, height=image.height,
                                 sharpness=sharpness, difference=difference, decoded_pixels_sha256=digest))
            last_thumbnail = thumbnail
            digests.add(digest)
            if len(accepted) >= args.max_frames:
                manifest["selection_stopped_at_max_frames"] = True
                break
        if len(accepted) < args.min_frames:
            raise ValueError(f"Only {len(accepted)} usable views; at least {args.min_frames} required. Review manifest.json and retake with more overlap/lighting.")
        manifest["status"] = "prepared"
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        raise
    finally:
        iterator.close()
        manifest["accepted_count"] = len(accepted)
        manifest["rejected_count"] = len(rejected)
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Photo folder or object orbit video")
    parser.add_argument("--output", type=Path, required=True, help="New folder; existing folders are never overwritten")
    parser.add_argument("--sample-every", type=float, default=0.5, help="Candidate video interval in seconds")
    parser.add_argument("--max-frames", type=int, default=120)
    parser.add_argument("--max-side", type=int, default=2048)
    parser.add_argument("--min-sharpness", type=float, default=30)
    parser.add_argument("--min-difference", type=float, default=2)
    parser.add_argument("--min-frames", type=int, default=10)
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0, help="Explicit clockwise rotation for video frames")
    args = parser.parse_args()
    if not all(math.isfinite(value) for value in (args.sample_every, args.min_sharpness, args.min_difference)):
        parser.error("Numeric options must be finite")
    if args.sample_every <= 0 or args.max_frames < args.min_frames or args.min_frames < 3 or args.max_side < 64 or min(args.min_sharpness, args.min_difference) < 0:
        parser.error("Require interval > 0, max_frames >= min_frames >= 3, max_side >= 64, nonnegative quality thresholds")
    try:
        manifest = prepare(args)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "prepared", "photo_count": manifest["accepted_count"],
                      "rejected_count": manifest["rejected_count"], "output": str(args.output.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
