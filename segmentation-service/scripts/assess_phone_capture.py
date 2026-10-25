#!/usr/bin/env python3
"""Check exported geometry against the phone fixture README (no Android edits)."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from housing_segmentation.captures.acceptance import assess_outputs
from housing_segmentation.captures.format import Capture

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('job_folder', type=Path)
parser.add_argument('--truth', type=Path, required=True)
args = parser.parse_args()
capture = Capture.load(args.job_folder / 'inputs')
report = json.loads((args.job_folder / 'outputs/report.json').read_text())
result = assess_outputs(args.job_folder, capture, json.loads(args.truth.read_text()), report)
(args.job_folder / 'outputs/phone-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['passes'] else 1)
