import json
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep

import numpy as np

from ..config import SERVICE_ROOT
from .format import matrix, column_major
from .geometry import register_camera_poses, transform_points


def run_object_capture(capture, masks_dir, output, check, timeout=120):
    if sys.platform != "darwin":
        raise RuntimeError("RealityKit Object Capture requires macOS")
    root = SERVICE_ROOT / "reconstruction"
    binary = root / ".build/object-capture"
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if not binary.exists() or binary.stat().st_mtime<(root/'ObjectCapture.swift').stat().st_mtime:
        subprocess.run(["/bin/bash", str(root / "build.sh")], check=True, timeout=90, capture_output=True, text=True)
    started = monotonic()
    with (output / "swift.log").open("w") as log:
        process = subprocess.Popen([str(binary), str((capture.root / "manifest.json").resolve()), str(Path(masks_dir).resolve()), str(output.resolve())], stdout=log, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                check()
                if monotonic() - started > timeout:
                    raise RuntimeError(f"Object Capture exceeded {timeout} seconds; using metric depth-fusion fallback")
                sleep(.1)
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    if process.returncode != 0 or not (output / "result.json").exists():
        tail = (output / "swift.log").read_text(errors="replace")[-2500:]
        raise RuntimeError(f"Object Capture failed (exit {process.returncode}): {tail}")
    result = json.loads((output / "result.json").read_text())
    source, target = [], []
    for frame in capture.manifest.frames:
        pose = result["poses"].get(str(frame.index))
        if pose is not None:
            source.append(matrix(pose)[:3, 3])
            target.append(matrix(frame.camera_to_anchor)[:3, 3])
    transform, registration = register_camera_poses(source, target)
    import open3d as o3d
    mesh = o3d.io.read_triangle_mesh(result["mesh"])
    if not len(mesh.triangles):
        raise RuntimeError("Object Capture OBJ conversion contains no triangles")
    mesh.vertices = o3d.utility.Vector3dVector(transform_points(np.asarray(mesh.vertices), transform))
    mesh.compute_vertex_normals()
    registration["similarity_to_anchor"] = transform.reshape(-1, order="F").tolist()
    registration["texture_cameras"] = {}
    for frame in capture.manifest.frames:
        pose = result["poses"].get(str(frame.index))
        if pose is None:
            continue
        aligned = transform @ matrix(pose)
        aligned[:3, :3] /= registration["scale"]
        camera = {"camera_to_anchor": column_major(aligned)}
        values = result.get("intrinsics", {}).get(str(frame.index))
        if values:
            k = np.asarray(values).reshape(3, 3, order="F")
            camera["intrinsics"] = dict(fx=float(k[0, 0]), fy=float(k[1, 1]), cx=float(k[0, 2]), cy=float(k[1, 2]))
        registration["texture_cameras"][str(frame.index)] = camera
    return mesh, registration
