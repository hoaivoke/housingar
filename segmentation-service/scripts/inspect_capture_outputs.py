#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from housing_segmentation.captures.format import Capture
from housing_segmentation.captures.validation import synthetic_novel_view_coverage, validate_phone_glb

parser = argparse.ArgumentParser(description="Reload phone GLBs and optionally evaluate synthetic novel-view coverage")
parser.add_argument("job_folder", type=Path)
parser.add_argument("--synthetic-source", type=Path)
args = parser.parse_args()
report = {name: validate_phone_glb(args.job_folder / "outputs" / name) for name in ("object.glb", "background.glb")}
if args.synthetic_source:
    capture = Capture.load(args.synthetic_source)
    truth = json.loads((args.synthetic_source / "ground_truth.json").read_text())
    report["novel_view_coverage"] = synthetic_novel_view_coverage(capture, args.job_folder / "outputs/background.glb", truth)
path = args.job_folder / "outputs/validation.json"
path.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
