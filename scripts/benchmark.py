#!/usr/bin/env python3
"""Repository-level entry point for the segmentation benchmark."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "segmentation-service" / "scripts" / "benchmark.py"), run_name="__main__")
