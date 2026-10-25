#!/usr/bin/env python3
"""Run the same capture pipeline offline; no Android or live API is required."""
import argparse
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import shutil
import sys
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from housing_segmentation.captures.format import Capture, required_files
from housing_segmentation.captures.jobs import atomic_json
from housing_segmentation.captures.pipeline import PipelineOptions, process_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Capture folder containing manifest.json")
    parser.add_argument("--output", type=Path, help="New job folder; default data/captures/<capture_id>")
    parser.add_argument("--mask-backend", choices=("sam2", "depth_bbox"), default="sam2")
    parser.add_argument("--model", choices=("tiny", "small"), default="tiny")
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--no-object-capture", action="store_true")
    parser.add_argument("--geometry-backend", choices=("auto","photo","depth"),default="auto")
    parser.add_argument("--object-capture-timeout", type=float, default=900)
    parser.add_argument("--inpainting", choices=("lama", "opencv"), default="lama")
    args = parser.parse_args()
    capture = Capture.load(args.folder)
    work = (args.output or ROOT / "data/captures" / capture.manifest.capture_id).resolve()
    if work.exists():
        parser.error("Output already exists; choose a new job folder")
    work.parent.mkdir(parents=True, exist_ok=True)
    worker_lock = (work.parent / ".worker.lock").open("a")
    try:
        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("A capture worker is using this output root; use HTTP or stop that worker first")
    (work / "inputs").mkdir(parents=True)
    for name in required_files(capture.manifest) | {"manifest.json"}:
        target = work / "inputs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(capture.root / name, target)
    job = {"job_id": uuid4().hex, "capture_id": capture.manifest.capture_id, "state": "queued", "progress": 0.,
           "message": "Offline capture", "error": None, "outputs": {"object_glb": None, "background_glb": None, "report": None}}
    def update(state, progress, message):
        job.update(state=state, progress=progress, message=message)
        atomic_json(work / "job.json", job)
        event = {"state": state, "progress": progress, "message": message}
        print(json.dumps(event), flush=True)
        with (work / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
    options = PipelineOptions(mask_backend=args.mask_backend, model=f"facebook/sam2.1-hiera-{args.model}", device=args.device,
                              object_capture=not args.no_object_capture, object_capture_timeout=args.object_capture_timeout,
                              inpainting=args.inpainting,geometry_backend=args.geometry_backend)
    started = perf_counter()
    try:
        report = process_capture(work / "inputs", work, update, lambda: None, options=options)
        job["outputs"] = {"object_glb": str(work / "outputs/object.glb"), "background_glb": str(work / "outputs/background.glb"), "report": str(work / "outputs/report.json")}
        update("done", 1., "Offline reconstruction complete")
        print(json.dumps({"elapsed_seconds": perf_counter() - started, "method": report["method"],
                          "dimensions_m": report["object_dimensions_m"], "outputs": job["outputs"]}), flush=True)
    except BaseException as exc:
        job["error"] = str(exc)
        update("cancelled" if isinstance(exc, KeyboardInterrupt) else "failed", job["progress"], "Offline reconstruction did not complete")
        raise
    finally:
        worker_lock.close()


if __name__ == "__main__":
    main()
