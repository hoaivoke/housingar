#!/usr/bin/env python3
"""Benchmark each model in a fresh process, including synchronized MPS timings."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import perf_counter
import traceback

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("HF_HOME", str(SERVICE_ROOT / ".cache" / "huggingface"))

import numpy as np
import psutil

from housing_segmentation.backends import make_backend
from housing_segmentation.config import Settings
from housing_segmentation.engine import SegmentationEngine, decode_image


def summarize(values):
    return {
        "mean": float(np.mean(values)), "median": float(np.median(values)),
        "p95": float(np.percentile(values, 95)), "max": float(max(values)),
    }


class MemorySampler:
    def __init__(self):
        self.peak_rss = 0
        self.peak_mps_allocated = 0
        self.peak_mps_driver = 0
        self.backend = None
        self._stop = Event()
        self._process = psutil.Process()

    def sample(self):
        self.peak_rss = max(self.peak_rss, self._process.memory_info().rss)
        if self.backend is not None and self.backend.device == "mps":
            mps = self.backend.torch.mps
            self.peak_mps_allocated = max(self.peak_mps_allocated, mps.current_allocated_memory())
            self.peak_mps_driver = max(self.peak_mps_driver, mps.driver_allocated_memory())

    def _poll(self):
        while not self._stop.wait(0.01):
            self.sample()

    def __enter__(self):
        self.sample()
        self._thread = Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join()
        self.sample()

    def result(self):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        return {
            "peak_rss_mib": max(self.peak_rss, peak_bytes) / (1024 ** 2),
            "peak_mps_allocated_mib": self.peak_mps_allocated / (1024 ** 2) if self.backend.device == "mps" else None,
            "peak_mps_driver_mib": self.peak_mps_driver / (1024 ** 2) if self.backend.device == "mps" else None,
            "scope": "fresh worker process; includes model loading and warmup; MPS memory sampled every 10 ms",
        }


def benchmark_worker(args):
    images = sorted(p for p in args.folder.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not images:
        raise ValueError(f"No JPEG/PNG images in {args.folder}")
    settings = Settings(backend=args.backend, model=args.worker_model if args.backend == "sam2" else "tiny", device=args.device)
    records = []
    with MemorySampler() as memory:
        start = perf_counter()
        backend = make_backend(settings)
        memory.backend = backend
        backend.synchronize()
        load_ms = (perf_counter() - start) * 1000
        engine = SegmentationEngine(settings, backend)
        for image_index, path in enumerate(images):
            content = path.read_bytes()
            rgb = decode_image(content, settings)
            height, width = rgb.shape[:2]
            points = [[width // 2, height // 2, 1]]
            for _ in range(args.warmup):
                engine.clear_cache()
                engine.segment(content, "warmup", "benchmark", points)
            for run in range(args.runs):
                engine.clear_cache()
                frame = f"{image_index}-{run}"
                initial = engine.segment(content, frame, "benchmark", points)
                correction = engine.segment(content, frame, "benchmark", points + [[min(width - 1, width // 2 + 1), height // 2, 1]])
                if not correction["embedding_cached"] or correction["timing_ms"]["embed"] != 0:
                    raise RuntimeError("repeat-prompt embedding cache did not work")
                records.append({
                    "image": str(path.resolve()), "width": width, "height": height, "run": run + 1,
                    "timing_ms": initial["timing_ms"], "cached_click_ms": correction["timing_ms"],
                })
        engine.clear_cache()
    return {
        "status": "ok", "backend": backend.name, "model": backend.model, "device": backend.device,
        "model_load_ms": load_ms, "runs_per_image": args.runs, "image_count": len(images),
        "warmup_per_image": args.warmup, "memory": memory.result(),
        "torch_version": backend.torch.__version__ if backend.name == "sam2" else None,
        "torch_cpu_threads": backend.torch.get_num_threads() if backend.name == "sam2" else None,
        "summary_ms": {phase: summarize([r["timing_ms"][phase] for r in records]) for phase in ("decode", "embed", "predict", "postprocess", "total")},
        "cached_click_summary_ms": summarize([r["cached_click_ms"]["total"] for r in records]),
        "target_ms": 3000, "all_results_under_target": all(r["timing_ms"]["total"] < 3000 for r in records),
        "timing_scope": "engine decode + embed + predict + mask serialization; excludes HTTP, disk persistence, model loading/download",
        "samples": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder of room photographs (JPEG/PNG), searched recursively")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--backend", choices=("sam2", "floodfill"), default="sam2")
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--models", nargs="+", choices=("tiny", "small"), default=["tiny", "small"])
    parser.add_argument("--output", type=Path, default=SERVICE_ROOT / "data" / "benchmark.json")
    parser.add_argument("--image-source", default="user-supplied images; not independently classified")
    parser.add_argument("--worker-model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0 or not args.folder.is_dir():
        parser.error("folder must exist, runs must be positive, warmup must be nonnegative")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker_model:
        try:
            report = benchmark_worker(args)
        except Exception as exc:
            traceback.print_exc()
            args.output.write_text(json.dumps({"status": "error", "model": args.worker_model, "error": str(exc)}, indent=2) + "\n")
            return 1
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return 0
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "python": platform.python_version(), "image_source": args.image_source, "results": [],
    }
    failed = False
    for model in args.models if args.backend == "sam2" else ["floodfill"]:
        print(f"Benchmarking {model} ({args.backend}, device={args.device})...", flush=True)
        with tempfile.TemporaryDirectory(prefix="housing-ar-benchmark-") as temp:
            output = Path(temp) / "result.json"
            command = [sys.executable, str(Path(__file__).resolve()), str(args.folder.resolve()),
                       "--worker-model", model, "--backend", args.backend, "--device", args.device,
                       "--runs", str(args.runs), "--warmup", str(args.warmup), "--output", str(output)]
            process = subprocess.run(command)
            result = json.loads(output.read_text()) if output.exists() else {"status": "error", "model": model, "error": f"worker exited {process.returncode}"}
        report["results"].append(result)
        failed |= process.returncode != 0
        if result["status"] == "ok":
            stats = result["summary_ms"]
            print(f"  {result['device']}: embed={stats['embed']['mean']:.1f} ms, "
                  f"predict={stats['predict']['mean']:.1f} ms, total={stats['total']['mean']:.1f} ms, "
                  f"peak RSS={result['memory']['peak_rss_mib']:.1f} MiB", flush=True)
        else:
            print(f"  FAILED: {result['error']}", file=sys.stderr, flush=True)
        # Preserve partial results if a subsequent model is interrupted.
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Results: {args.output.resolve()}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
