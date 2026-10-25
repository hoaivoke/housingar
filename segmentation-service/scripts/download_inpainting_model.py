#!/usr/bin/env python3
"""Download the recorded LaMa checkpoint without installing another model package."""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from housing_segmentation.captures.texturing import LAMA_MD5, LAMA_SOURCE, LAMA_URL

destination = ROOT / ".cache/inpainting/big-lama.pt"
destination.parent.mkdir(parents=True, exist_ok=True)
if not destination.exists():
    temporary = destination.with_suffix(".download")
    try:
        urllib.request.urlretrieve(LAMA_URL, temporary)
        if hashlib.md5(temporary.read_bytes()).hexdigest() != LAMA_MD5:
            raise SystemExit("Checkpoint integrity check failed")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
data = destination.read_bytes()
if hashlib.md5(data).hexdigest() != LAMA_MD5:
    raise SystemExit("Existing checkpoint integrity check failed")
print(json.dumps({"path": str(destination), "source": LAMA_SOURCE, "url": LAMA_URL,
                  "sha256": hashlib.sha256(data).hexdigest(), "source_code_license": "Apache-2.0"}, indent=2))
