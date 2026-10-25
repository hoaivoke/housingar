#!/usr/bin/env python3
"""Rebuild the three local phone recordings using saved real-SAM masks.

This deliberately preserves the original jobs and records hashes of every image,
mask and calibration used. It does not download data or run synthetic masks.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter

import open3d as o3d

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from housing_segmentation.captures.format import Capture, column_major
from housing_segmentation.captures.geometry import object_local_frame
from housing_segmentation.captures.glb import read_accessor, read_glb, write_glb
from housing_segmentation.captures.image_geometry import (
    reconstruct_images,
    texture_cameras,
)
from housing_segmentation.captures.texturing import bake_texture
from housing_segmentation.captures.validation import validate_phone_glb
from housing_segmentation.scans.format import Scan
from housing_segmentation.scans.tapped import MaskFiles

RECORDINGS = [
    ("black-chair", "scans/6fb50237_scan_93413134"),
    ("second-chair", "scans/baae385b_scan_91807239"),
    ("wooden-chair", "captures/d7ce4323_obj2_70861434"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", choices=[r[0] for r in RECORDINGS])
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for name, relative in RECORDINGS:
        if args.only and name != args.only:
            continue
        job = ROOT / "data" / relative
        out = args.output / name
        if (out / "result.json").exists():
            raise ValueError(f"Result exists: {out}")
        out.mkdir(parents=True, exist_ok=True)
        capture = (Scan if relative.startswith("scans") else Capture).load(
            job / "inputs"
        )
        masks = job / (
            "masks/raw/obj_01" if relative.startswith("scans") else "masks/raw_masks"
        )
        source_report = job / (
            "outputs/obj_01/report.json"
            if relative.startswith("scans")
            else "outputs/report.json"
        )
        previous = json.loads(source_report.read_text())
        provenance = (
            previous["segmentation"]["video_tracking"]
            if relative.startswith("scans")
            else previous["masking"]
        )
        if provenance.get("backend") not in ("sam2", "sam2_video") or provenance.get(
            "model"
        ) not in ("facebook/sam2.1-hiera-tiny", "facebook/sam2.1-hiera-small"):
            raise ValueError("Regression requires recorded official SAM masks")
        old_glb = job / (
            "outputs/obj_01/object.glb"
            if relative.startswith("scans")
            else "outputs/object.glb"
        )
        document, binary = read_glb(old_glb)
        old_mesh = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(
                read_accessor(document, binary, 0).astype(float)
            ),
            o3d.utility.Vector3iVector(
                read_accessor(document, binary, 3).reshape(-1, 3).astype(int)
            ),
        )
        old_mesh.remove_duplicated_vertices().remove_degenerate_triangles().remove_unreferenced_vertices()
        old_components = len(old_mesh.cluster_connected_triangles()[1])
        checksum = hashlib.sha256((capture.root / "manifest.json").read_bytes())
        for frame in capture.manifest.frames:
            for path in [capture.root / frame.image, masks / f"{frame.index:04d}.png"]:
                checksum.update(path.name.encode())
                checksum.update(path.read_bytes())
        print(
            json.dumps(
                {
                    "recording": name,
                    "stage": "photogrammetry",
                    "input_sha256": checksum.hexdigest(),
                }
            ),
            flush=True,
        )
        started = perf_counter()
        point = (
            capture.manifest.objects[0].point_anchor
            if hasattr(capture.manifest, "objects")
            else None
        )
        mesh, geometry = reconstruct_images(
            capture,
            masks,
            out / "photogrammetry",
            lambda: None,
            timeout=args.timeout,
            point_anchor=point,
        )
        print(
            json.dumps(
                {
                    "recording": name,
                    "stage": "texturing",
                    "geometry": geometry["stages"]["final"],
                }
            ),
            flush=True,
        )
        texture_started = perf_counter()
        arrays, texture = bake_texture(
            texture_cameras(capture, geometry),
            MaskFiles(masks, capture.manifest.frames),
            mesh,
            lambda: None,
            object_only=True,
            inpainting="opencv",
            device="cpu",
            visibility="mesh",
        )
        _, placement, dimensions = object_local_frame(
            mesh.vertices, capture.manifest.anchor_to_world
        )
        v, n, uv, f, image = arrays
        basis = placement[:3, :3]
        origin = placement[:3, 3]
        write_glb(out / "object.glb", (v - origin) @ basis, n @ basis, uv, f, image)
        contract = validate_phone_glb(out / "object.glb", require_closed=True)
        texture["seconds"] = perf_counter() - texture_started
        before = previous.get("proposal", previous)
        result = {
            "recording": relative,
            "input_sha256": checksum.hexdigest(),
            "mask_provenance": {
                key: provenance.get(key) for key in ("backend", "model", "device")
            },
            "frames": len(capture.manifest.frames),
            "before": {
                "dimensions_m": before.get(
                    "dimensions_m", before.get("object_dimensions_m")
                ),
                "observed_fraction": before.get("quality", {}).get(
                    "observed_fraction",
                    previous.get("object_texture", {}).get("observed_texel_fraction"),
                ),
                "components": old_components,
                "depth_components": previous.get(
                    "fusion", previous.get("object_depth_comparison", {})
                )
                .get("component_validation", {})
                .get("retained"),
            },
            "method": geometry["method"],
            "geometry": geometry,
            "texture": texture,
            "dimensions_m": dimensions.tolist(),
            "object_to_anchor": column_major(placement),
            "contract": contract,
            "total_seconds": perf_counter() - started,
            "low_quality": texture["low_quality"]
            or geometry["geometry_completeness"]["low_quality"],
        }
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        results.append(result)
        if result["low_quality"]:
            raise RuntimeError(
                f"{name} did not pass the object reconstruction quality targets; see its result.json"
            )
        print(
            json.dumps(
                {
                    "recording": name,
                    "stage": "done",
                    "dimensions_m": dimensions.tolist(),
                    "observed_fraction": texture["observed_texel_fraction"],
                    "seconds": result["total_seconds"],
                }
            ),
            flush=True,
        )
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
