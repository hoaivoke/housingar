#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from housing_segmentation.captures.synthetic import generate_capture, zip_capture

parser = argparse.ArgumentParser(description="Render a metric capture.v1 fixture offscreen on CPU")
parser.add_argument("folder", type=Path)
parser.add_argument("--frames", type=int, default=36)
parser.add_argument("--width", type=int, default=320)
parser.add_argument("--height", type=int, default=240)
parser.add_argument("--capture-id", default="synthetic_box")
parser.add_argument("--tilted-anchor", action="store_true")
args = parser.parse_args()
generate_capture(args.folder, frames=args.frames, width=args.width, height=args.height, capture_id=args.capture_id, tilt_anchor=args.tilted_anchor)
zip_capture(args.folder, args.folder.with_suffix(".zip"))
print(args.folder.resolve())
