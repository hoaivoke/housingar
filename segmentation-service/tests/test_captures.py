from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import struct
from threading import Event, Lock
from time import monotonic, sleep
import zipfile

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from housing_segmentation.app import create_app
from housing_segmentation.config import Settings
from housing_segmentation.engine import InputError
from housing_segmentation.captures.format import Capture, extract_archive, matrix, parse_manifest, validate_folder
from housing_segmentation.captures.geometry import anchor_depth_points, depth_to_image, object_local_frame, project, register_camera_poses, transform_points
from housing_segmentation.captures.glb import read_accessor, read_glb
from housing_segmentation.captures.jobs import JobManager, atomic_json
from housing_segmentation.captures.masking import propagate_masks
from housing_segmentation.captures.pipeline import PipelineOptions
from housing_segmentation.captures.synthetic import generate_capture, zip_capture


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    root = tmp_path_factory.mktemp("capture-v1") / "capture"
    manifest = generate_capture(root, frames=24, width=240, height=180)
    archive = root.parent / "capture.zip"
    zip_capture(root, archive)
    return root, manifest, archive


@pytest.fixture
def capture_copy(synthetic, tmp_path):
    root, manifest, _ = synthetic
    target = tmp_path / "capture"
    shutil.copytree(root, target)
    return target, deepcopy(manifest)


def post_capture(client, manifest, archive):
    return client.post("/v1/captures", data={"manifest": json.dumps(manifest)}, files={"archive": ("capture.zip", archive.read_bytes(), "application/zip")})


def wait_job(client, job_id, states, timeout=15):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = client.get(f"/v1/jobs/{job_id}").json()
        if result["state"] in states:
            return result
        sleep(.01)
    raise AssertionError(f"Job did not reach {states}: {result}")


def test_geometry_roundtrip_and_log_listing(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, backend="floodfill"))
    payload = {"session_id": "s1", "selection_id": "selected_1", "polygon": [[0, 0, 0], [1, 0, 0], [0, 1, 0]]}
    with TestClient(app) as client:
        response = client.post("/v1/geometry", json=payload)
        assert response.status_code == 200
        assert response.json()["path"] == "geometry/s1_selected_1.json"
        assert json.loads((tmp_path / response.json()["path"]).read_text()) == payload
        payload["revision"] = 2
        assert client.post("/v1/geometry", json=payload).status_code == 200
        assert json.loads((tmp_path / response.json()["path"]).read_text())["revision"] == 2
        assert len(client.get("/v1/logs").json()["geometry"]) == 1
        assert client.post("/v1/geometry", json={"session_id": "../bad", "selection_id": "x"}).status_code == 422
        assert client.post("/v1/geometry", json={"session_id": "s1"}).status_code == 422


def test_valid_capture_and_depth_mapping(synthetic, tmp_path):
    root, payload, archive = synthetic
    manifest = parse_manifest(payload)
    extract_archive(archive, tmp_path / "extracted", manifest)
    capture = Capture.load(root)
    frame = manifest.frames[0]
    assert (frame.depth.width, frame.depth.height) != (frame.width, frame.height)
    assert frame.depth.to_image[2:] != [0, 0]
    points = anchor_depth_points(frame, capture.depth(frame))
    pixels, z = project(points.reshape(-1, 3), frame)
    x, y = depth_to_image(frame)
    valid = capture.depth(frame).reshape(-1) > 0
    assert np.max(np.abs(np.floor(pixels[valid]) - np.stack([x, y], -1).reshape(-1, 2)[valid])) <= 1
    assert np.allclose(z[valid], capture.depth(frame).reshape(-1)[valid] / 1000)


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(schema="housingar.capture.v2"),
    lambda m: m.update(capture_id="../escape"),
    lambda m: m.update(frames=m["frames"][:11]),
    lambda m: m["frames"][0]["camera_to_anchor"].__setitem__(0, float("nan")),
    lambda m: m["frames"][0]["camera_to_anchor"].__setitem__(0, 2),
    lambda m: m["frames"][0].update(image="../../outside.jpg"),
    lambda m: m["frames"][0]["depth"].update(to_image=[2, 2, 900, 900]),
    lambda m: m["frames"][0].update(index=m["frames"][1]["index"]),
    lambda m: m["seed"].update(frame_index=999),
    lambda m: m["seed"].update(points=[[999, 0, 1]]),
    lambda m: m["seed"]["bbox_anchor"].update(max=m["seed"]["bbox_anchor"]["min"]),
])
def test_manifest_rejections(synthetic, mutation):
    payload = deepcopy(synthetic[1])
    mutation(payload)
    with pytest.raises(InputError):
        parse_manifest(payload)


@pytest.mark.parametrize("kind", ["missing_rgb", "rgb_size", "depth_size", "confidence_size", "mask_size", "mask_mode"])
def test_capture_file_rejections(capture_copy, kind):
    root, payload = capture_copy
    frame = payload["frames"][0]
    if kind == "missing_rgb": (root / frame["image"]).unlink()
    if kind == "rgb_size": Image.new("RGB", (20, 30)).save(root / frame["image"])
    if kind == "depth_size": (root / frame["depth"]["file"]).write_bytes(b"\0")
    if kind == "confidence_size": (root / frame["confidence"]["file"]).write_bytes(b"\0")
    if kind == "mask_size": Image.new("L", (3, 3)).save(root / "seed_mask.png")
    if kind == "mask_mode": Image.new("RGB", (frame["width"], frame["height"])).save(root / "seed_mask.png")
    with pytest.raises(InputError): validate_folder(root, parse_manifest(payload))


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "frames\\bad.jpg", "frames/./bad.jpg"])
def test_unsafe_zip_paths(synthetic, tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(name, b"unsafe")
    with pytest.raises(InputError, match="path"):
        extract_archive(archive, tmp_path / "extracted", parse_manifest(synthetic[1]))


def test_zip_symlink_and_missing_files(synthetic, tmp_path):
    archive = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        entry = zipfile.ZipInfo("frames/link.jpg")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        stream.writestr(entry, "outside")
    with pytest.raises(InputError, match="symlink"):
        extract_archive(archive, tmp_path / "extracted", parse_manifest(synthetic[1]))
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("seed_mask.png", b"no other files")
    with pytest.raises(InputError, match="missing"):
        extract_archive(archive, tmp_path / "extracted", parse_manifest(synthetic[1]))


def test_archive_over_400_mb_is_rejected_without_reading(synthetic, tmp_path):
    archive = tmp_path / "oversize.zip"
    with archive.open("wb") as stream:
        stream.truncate(400_000_001)
    with pytest.raises(InputError) as error:
        extract_archive(archive, tmp_path / "extracted", parse_manifest(synthetic[1]))
    assert error.value.status_code == 413


def test_similarity_registration_rejects_outliers():
    rng = np.random.default_rng(9)
    source = rng.normal(size=(30, 3))
    angle = .45
    rotation = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0], [-np.sin(angle), 0, np.cos(angle)]])
    expected = np.eye(4)
    expected[:3, :3] = rotation * .6
    expected[:3, 3] = [.2, .5, -.4]
    target = transform_points(source, expected)
    target[-4:] += 3
    recovered, report = register_camera_poses(source, target)
    assert np.allclose(recovered, expected, atol=1e-8)
    assert report["inliers"] == 26 and report["rms_m"] < 1e-8
    assert abs(report["scale"] - .6) < 1e-8


def test_gravity_aligned_placement_with_rotated_anchor():
    angle = .4
    anchor = np.eye(4)
    anchor[:3, :3] = [[1, 0, 0], [0, np.cos(angle), -np.sin(angle)], [0, np.sin(angle), np.cos(angle)]]
    anchor[:3, 3] = [.1, .2, .3]
    world = np.array([[x, y, z] for x in (-.2, .2) for y in (.6, .9) for z in (-.15, .15)])
    in_anchor = transform_points(world, np.linalg.inv(anchor))
    local, pose, dimensions = object_local_frame(in_anchor, anchor.reshape(-1, order="F"))
    assert np.allclose(transform_points(local, pose), in_anchor)
    assert np.allclose(anchor[:3, :3] @ pose[:3, 1], [0, 1, 0])
    assert np.allclose(dimensions, [.4, .3, .3]) and np.isclose(local[:, 1].min(), 0)


def test_sam_video_plumbing_with_seed_and_bidirectional_propagation(synthetic, tmp_path):
    # Exercises the official-predictor call boundary without a model download.
    import torch
    capture = Capture.load(synthetic[0])
    calls = []
    class Predictor:
        def init_state(self, **kwargs): calls.append(("init", kwargs)); return {}
        def add_new_mask(self, state, **kwargs): calls.append(("seed", kwargs))
        def add_new_points_or_box(self, state, **kwargs): calls.append(("box", kwargs))
        def propagate_in_video(self, state, start_frame_idx, reverse=False, **kwargs):
            calls.append(("propagate", reverse))
            indexes = range(start_frame_idx, -1, -1) if reverse else range(start_frame_idx, len(capture.manifest.frames))
            for i in indexes:
                with Image.open(capture.root / f"truth_masks/{i:04d}.png") as image:
                    values = np.where(np.asarray(image) > 0, 10., -10.).astype(np.float32)
                if i == 3: values[:] = -10.  # Force the projected-box recovery path.
                yield i, [1], torch.from_numpy(values)[None, None]
    masks, report = propagate_masks(capture, tmp_path / "masks", lambda: None, predictor=Predictor(), device="cpu")
    assert len(masks) == 24 and report["backend"] == "sam2"
    assert calls[0][0] == "init" and any(c[0] == "seed" for c in calls)
    assert ("propagate", True) in calls and (tmp_path / "masks/contact_sheet.jpg").is_file()
    assert any(call[0] == "box" and call[1]["frame_idx"] == 3 for call in calls)


def test_job_lifecycle_serial_worker_cancel_and_downloads(synthetic, tmp_path):
    entered, release = Event(), Event()
    running, maximum = 0, 0
    guard = Lock()
    def runner(inputs, folder, update, check, **kwargs):
        nonlocal running, maximum
        with guard: running += 1; maximum = max(maximum, running)
        try:
            update("masking", .2, "Test masking")
            entered.set()
            while not release.wait(.01): check()
            check()
            (folder / "outputs").mkdir()
            for name in ("object.glb", "background.glb", "report.json"):
                (folder / "outputs" / name).write_bytes(b"{}")
            update("exporting", .9, "Test export")
        finally:
            with guard: running -= 1
    app = create_app(Settings(backend="floodfill", data_dir=tmp_path), capture_runner=runner)
    with TestClient(app) as client:
        first = post_capture(client, synthetic[1], synthetic[2])
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert entered.wait(5)
        assert client.get("/health").status_code == 200
        assert client.get(f"/v1/jobs/{job_id}/files/object.glb").status_code == 409
        second_manifest = deepcopy(synthetic[1]); second_manifest["capture_id"] = "second"
        second = post_capture(client, second_manifest, synthetic[2]).json()["job_id"]
        assert client.get(f"/v1/jobs/{second}").json()["state"] == "queued"
        assert client.post(f"/v1/jobs/{second}/cancel").json()["state"] == "cancelled"
        assert post_capture(client, synthetic[1], synthetic[2]).status_code == 409
        release.set()
        done = wait_job(client, job_id, {"done", "failed"})
        assert done["state"] == "done" and done["progress"] == 1 and done["error"] is None
        for url in done["outputs"].values(): assert client.get(url).status_code == 200
        assert client.get(f"/v1/jobs/{job_id}/files/anything.txt").status_code == 404
        assert client.get("/v1/jobs/missing").status_code == 404
        assert client.post(f"/v1/jobs/{job_id}/cancel").status_code == 409
        assert maximum == 1
    assert json.loads((tmp_path / "captures" / synthetic[1]["capture_id"] / "job.json").read_text())["state"] == "done"


def test_running_job_cancellation(synthetic, tmp_path):
    entered = Event()
    def runner(inputs, folder, update, check, **kwargs):
        update("masking", .1, "Masking")
        entered.set()
        while True: check(); sleep(.01)
    with TestClient(create_app(Settings(data_dir=tmp_path), capture_runner=runner)) as client:
        job = post_capture(client, synthetic[1], synthetic[2]).json()["job_id"]
        assert entered.wait(5)
        assert client.post(f"/v1/jobs/{job}/cancel").status_code == 200
        cancelled = wait_job(client, job, {"cancelled"})
        assert cancelled["outputs"] == {"object_glb": None, "background_glb": None, "report": None}


def test_processing_failure_is_persisted_without_downloads(synthetic, tmp_path):
    def fail(*args, **kwargs): raise RuntimeError("Synthetic reconstruction failure")
    with TestClient(create_app(Settings(data_dir=tmp_path), capture_runner=fail)) as client:
        job = post_capture(client, synthetic[1], synthetic[2]).json()["job_id"]
        result = wait_job(client, job, {"failed"})
        assert result["error"] == "Synthetic reconstruction failure"
        assert all(value is None for value in result["outputs"].values())
        assert client.get(f"/v1/jobs/{job}/files/object.glb").status_code == 409
    state = json.loads((tmp_path / "captures" / synthetic[1]["capture_id"] / "job.json").read_text())
    assert state["state"] == "failed"


def test_failed_job_and_restart_recovery(tmp_path):
    root = tmp_path / "captures"
    folder = root / "interrupted"
    folder.mkdir(parents=True)
    atomic_json(folder / "job.json", {"capture_id": "interrupted", "job_id": "lost-job", "state": "texturing", "progress": .7, "message": "busy", "error": None, "outputs": {}})
    manager = JobManager(root)
    try:
        state = manager.get("lost-job")
        assert state["state"] == "failed" and "stopped" in state["error"]
        assert all(value is None for value in state["outputs"].values())
    finally: manager.close()
    recovered = JobManager(root)
    try: assert recovered.get("lost-job")["state"] == "failed"
    finally: recovered.close()


def test_upload_rejects_bad_inputs_with_4xx(synthetic, tmp_path, monkeypatch):
    import housing_segmentation.captures.jobs as jobs
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        manifest = deepcopy(synthetic[1]); manifest["frames"] = manifest["frames"][:2]
        assert post_capture(client, manifest, synthetic[2]).status_code == 422
        assert client.post("/v1/captures", data={"manifest": "{"}, files={"archive": ("x.zip", b"no")}).status_code == 400
        assert client.post("/v1/captures", data={"manifest": json.dumps(synthetic[1])}).status_code == 422
        monkeypatch.setattr(jobs, "MAX_ARCHIVE_BYTES", 1024)
        assert post_capture(client, synthetic[1], synthetic[2]).status_code == 413


@pytest.fixture(scope="module")
def reconstructed(synthetic, tmp_path_factory):
    root = tmp_path_factory.mktemp("metric-reconstruction")
    options = PipelineOptions(mask_backend="depth_bbox", device="cpu", object_capture=False, inpainting="opencv")
    with TestClient(create_app(Settings(data_dir=root, backend="floodfill"), capture_options=options)) as client:
        accepted = post_capture(client, synthetic[1], synthetic[2])
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        deadline = monotonic() + 180
        while monotonic() < deadline:
            state = client.get(f"/v1/jobs/{job_id}").json()
            assert client.get("/health").status_code == 200
            if state["state"] in {"done", "failed"}: break
            sleep(.1)
        assert state["state"] == "done", state
        for url in state["outputs"].values(): assert client.get(url).status_code == 200
    work = root / "captures" / synthetic[1]["capture_id"]
    report = json.loads((work / "outputs/report.json").read_text())
    pid = json.loads((work / "worker-pid.json").read_text())
    assert pid["pid"] != os.getpid() and pid["parent_pid"] == os.getpid()
    return work, report, synthetic


def test_isolated_worker_cancellation_stops_child(synthetic, tmp_path):
    options = PipelineOptions(mask_backend="depth_bbox", device="cpu", object_capture=False, inpainting="opencv")
    with TestClient(create_app(Settings(data_dir=tmp_path), capture_options=options)) as client:
        job = post_capture(client, synthetic[1], synthetic[2]).json()["job_id"]
        pid_file = tmp_path / "captures" / synthetic[1]["capture_id"] / "worker-pid.json"
        deadline = monotonic() + 10
        while not pid_file.exists() and monotonic() < deadline: sleep(.01)
        assert pid_file.is_file()
        pid = json.loads(pid_file.read_text())["pid"]
        assert pid != os.getpid()
        client.post(f"/v1/jobs/{job}/cancel").raise_for_status()
        assert wait_job(client, job, {"cancelled"})["state"] == "cancelled"
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)


def test_metric_mesh_and_object_to_anchor(reconstructed):
    work, report, synthetic = reconstructed
    expected = np.array([.4, .3, .3])
    assert np.max(np.abs(np.asarray(report["object_dimensions_m"]) - expected)) < .02
    document, binary = read_glb(work / "outputs/object.glb")
    local = read_accessor(document, binary, 0)
    assert abs(local[:, 1].min()) < .002
    assert np.max(np.abs(local.min(0)[[0, 2]] + local.max(0)[[0, 2]])) < .002
    in_anchor = transform_points(local, matrix(report["object_to_anchor"]))
    assert np.max(np.abs(in_anchor.min(0) - [-.2, .61, -.15])) < .02
    assert np.max(np.abs(in_anchor.max(0) - [.2, .91, .15])) < .02
    assert report["method"] == "depth_fusion" and report["frames_used"] >= 12


@pytest.mark.parametrize("filename", ["object.glb", "background.glb"])
def test_phone_glb_constraints_and_reload(reconstructed, filename):
    import trimesh
    work, report, _ = reconstructed
    path = work / "outputs" / filename
    document, binary = read_glb(path)
    assert document["asset"]["version"] == "2.0"
    assert len(document["nodes"]) == len(document["meshes"]) == len(document["images"]) == 1
    assert document["nodes"] == [{"mesh": 0}]
    assert "extensionsRequired" not in document
    primitives = document["meshes"][0]["primitives"]
    assert len(primitives) == 1 and primitives[0]["mode"] == 4
    assert set(primitives[0]["attributes"]) == {"POSITION", "NORMAL", "TEXCOORD_0"}
    assert document["accessors"][3]["componentType"] == 5125
    indices = read_accessor(document, binary, 3)
    assert len(indices) // 3 <= 50000
    for index, kind in enumerate(("VEC3", "VEC3", "VEC2")):
        assert document["accessors"][index]["componentType"] == 5126
        assert document["accessors"][index]["type"] == kind
        assert np.isfinite(read_accessor(document, binary, index)).all()
    view = document["bufferViews"][document["images"][0]["bufferView"]]
    image = Image.open(BytesIO(binary[view["byteOffset"]:view["byteOffset"] + view["byteLength"]]))
    assert image.size == (2048, 2048) and image.format == "PNG"
    reloaded = trimesh.load(path, force="scene")
    assert len(reloaded.geometry) == 1


def test_background_is_3d_supported_geometry_and_quality_report(reconstructed):
    work, report, _ = reconstructed
    document, binary = read_glb(work / "outputs/background.glb")
    points = read_accessor(document, binary, 0)
    assert np.ptp(points[:, 1]) > .3 and np.ptp(points[:, 2]) > .3
    assert len(report["background_geometry"]["support_planes"]) >= 2
    texture = report["background_texture"]
    assert 0 <= texture["inpainted_texel_fraction"] <= 1
    assert abs(texture["inpainted_texel_fraction"] + texture["observed_texel_fraction"] - 1) < 1e-8
    assert report["low_quality"] == (texture["inpainted_texel_fraction"] > .4 or report["background_view_coverage"]["minimum_fraction"] < .98)
    assert texture["inpainting"]["backend"] in {"opencv_telea", "none_needed"}
    assert (work / "masks/contact_sheet.jpg").is_file()
    assert report["timings_seconds"]["background_depth_fusion"] > 0


def test_patch_covers_object_from_three_nearby_novel_views(reconstructed):
    from housing_segmentation.captures.validation import synthetic_novel_view_coverage
    work, report, synthetic = reconstructed
    capture = Capture.load(synthetic[0])
    truth = json.loads((synthetic[0] / "ground_truth.json").read_text())
    coverage = synthetic_novel_view_coverage(capture, work / "outputs/background.glb", truth)
    assert all(view["coverage_fraction"] > .98 for view in coverage), coverage
