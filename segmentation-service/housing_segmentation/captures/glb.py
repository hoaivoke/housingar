"""Minimal glTF 2.0 GLB writer matching the phone's deliberately small loader."""
from io import BytesIO
import json
from pathlib import Path
import struct

import numpy as np
from PIL import Image


def write_glb(path, positions, normals, uv, faces, texture):
    arrays = [np.asarray(positions, dtype="<f4"), np.asarray(normals, dtype="<f4"),
              np.asarray(uv, dtype="<f4"), np.asarray(faces, dtype="<u4").reshape(-1)]
    if len(faces) > 50000 or not len(faces):
        raise ValueError("GLB must contain 1–50000 triangles")
    if not all(np.isfinite(array).all() for array in arrays[:3]):
        raise ValueError("Non-finite GLB attribute")
    if not (len(positions) == len(normals) == len(uv)) or np.max(faces) >= len(positions):
        raise ValueError("GLB attributes/indices disagree")
    image_stream = BytesIO()
    texture = Image.fromarray(np.asarray(texture, dtype=np.uint8)).convert("RGB")
    if texture.size != (2048, 2048):
        texture = texture.resize((2048, 2048), Image.Resampling.LANCZOS)
    texture.save(image_stream, "PNG")
    binary = bytearray()
    views = []
    for index, data in enumerate([array.tobytes() for array in arrays] + [image_stream.getvalue()]):
        binary.extend(b"\0" * (-len(binary) % 4))
        view = {"buffer": 0, "byteOffset": len(binary), "byteLength": len(data)}
        if index < 4:
            view["target"] = 34963 if index == 3 else 34962
        views.append(view)
        binary.extend(data)
    accessors = []
    for index, (array, kind) in enumerate(zip(arrays, ["VEC3", "VEC3", "VEC2", "SCALAR"])):
        accessor = {"bufferView": index, "componentType": 5125 if index == 3 else 5126,
                    "count": len(array), "type": kind}
        if index == 0:
            accessor.update(min=array.min(0).tolist(), max=array.max(0).tolist())
        accessors.append(accessor)
    document = {
        "asset": {"version": "2.0", "generator": "housingar.capture.v1"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0, "mode": 4}]}],
        "materials": [{"doubleSided": True, "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0, "roughnessFactor": 1}}],
        "textures": [{"source": 0, "sampler": 0}], "samplers": [{"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": 4, "mimeType": "image/png"}],
        "buffers": [{"byteLength": len(binary)}], "bufferViews": views, "accessors": accessors,
    }
    encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    encoded += b" " * (-len(encoded) % 4)
    binary.extend(b"\0" * (-len(binary) % 4))
    header = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(encoded) + 8 + len(binary))
    Path(path).write_bytes(header + struct.pack("<I4s", len(encoded), b"JSON") + encoded + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def read_glb(path):
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError("Invalid GLB header")
    count, kind = struct.unpack_from("<I4s", data, 12)
    if kind != b"JSON":
        raise ValueError("Missing GLB JSON")
    document = json.loads(data[20:20 + count])
    binary_count, binary_kind = struct.unpack_from("<I4s", data, 20 + count)
    if binary_kind != b"BIN\0":
        raise ValueError("Missing GLB binary")
    binary = data[28 + count:28 + count + binary_count]
    return document, binary


def read_accessor(document, binary, index):
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[accessor["type"]]
    dtype = {5126: "<f4", 5125: "<u4"}[accessor["componentType"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    return np.frombuffer(binary, dtype=dtype, count=accessor["count"] * width, offset=offset).reshape(-1, width)
