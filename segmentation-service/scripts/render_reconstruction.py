#!/usr/bin/env python3
"""CPU side-by-side renders in the same camera, scale and anchor frame."""

import json
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from housing_segmentation.captures.format import matrix
from housing_segmentation.captures.geometry import transform_points
from housing_segmentation.captures.glb import read_accessor, read_glb


def load(path, placement):
    document, binary = read_glb(path)
    primitive = document["meshes"][0]["primitives"][0]
    vertices = read_accessor(document, binary, primitive["attributes"]["POSITION"])
    uv = read_accessor(document, binary, primitive["attributes"]["TEXCOORD_0"])
    faces = read_accessor(document, binary, primitive["indices"]).reshape(-1, 3)
    view = document["bufferViews"][document["images"][0]["bufferView"]]
    texture = np.asarray(
        Image.open(
            BytesIO(
                binary[view["byteOffset"] : view["byteOffset"] + view["byteLength"]]
            )
        ).convert("RGB")
    )
    sample = uv[faces].mean(1)
    x = (
        (sample[:, 0] * (texture.shape[1] - 1))
        .astype(int)
        .clip(0, texture.shape[1] - 1)
    )
    y = (
        (sample[:, 1] * (texture.shape[0] - 1))
        .astype(int)
        .clip(0, texture.shape[0] - 1)
    )
    # glTF image coordinates have the same top-left origin as the atlas.
    colors = texture[y, x]
    return transform_points(vertices, matrix(placement)), faces, colors


def render_pair(before, after, output, *, title, labels, camera, up):
    centre = (after[0].min(0) + after[0].max(0)) / 2
    forward = np.asarray(camera) - centre
    forward /= np.linalg.norm(forward)
    right = np.cross(up, forward)
    right /= np.linalg.norm(right)
    vertical = np.cross(forward, right)
    rotation = np.column_stack([right, vertical, forward])
    models = []
    for vertices, faces, colors in (before, after):
        models.append(((vertices - centre) @ rotation, faces, colors))
    projected = np.vstack([m[0][:, :2] for m in models])
    extent = np.ptp(projected, axis=0)
    image_centre = (projected.min(0) + projected.max(0)) / 2
    scale = min(630 / max(extent[0], 1e-6), 590 / max(extent[1], 1e-6))
    canvas = Image.new("RGB", (1500, 820), (244, 246, 248))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    draw.text((25, 20), title, fill=(20, 30, 40), font=font)
    for i, (v, f, colors) in enumerate(models):
        xy = (v[:, :2] - image_centre) * [scale, -scale] + [375 + i * 750, 440]
        normals = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
        lighting = 0.65 + 0.35 * np.abs(normals[:, 2]) / np.maximum(
            np.linalg.norm(normals, axis=1), 1e-9
        )
        colors = np.clip(colors.astype(float) * lighting[:, None] + 10, 0, 255).astype(
            np.uint8
        )
        for j in np.argsort(v[f, 2].mean(1)):
            draw.polygon([tuple(p) for p in xy[f[j]]], fill=tuple(colors[j]))
        draw.text((25 + i * 750, 65), labels[i], fill=(20, 30, 40), font=font)
    canvas.save(output)


def main():
    import argparse

    from regress_real_reconstruction import RECORDINGS

    from housing_segmentation.captures.geometry import gravity_frame

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, relative in RECORDINGS:
        result = json.loads((args.results / name / "result.json").read_text())
        job = ROOT / "data" / relative
        old = json.loads(
            (
                job
                / (
                    "outputs/obj_01/report.json"
                    if relative.startswith("scans")
                    else "outputs/report.json"
                )
            ).read_text()
        )
        previous = old.get("proposal", old)
        before = load(
            job
            / (
                "outputs/obj_01/object.glb"
                if relative.startswith("scans")
                else "outputs/object.glb"
            ),
            previous["object_to_anchor"],
        )
        after = load(args.results / name / "object.glb", result["object_to_anchor"])
        manifest = json.loads((job / "inputs/manifest.json").read_text())
        up = gravity_frame(manifest["anchor_to_world"])[:, 1]
        camera = matrix(manifest["frames"][0]["camera_to_anchor"])[:3, 3]
        render_pair(
            before,
            after,
            args.output / (name + ".png"),
            title=name.replace("-", " ").title(),
            camera=camera,
            up=up,
            labels=[
                f"Before: raw depth | {result['before']['observed_fraction']:.0%} observed texture",
                f"After: masked photos | {result['texture']['observed_texel_fraction']:.0%} observed texture",
            ],
        )


if __name__ == "__main__":
    main()
