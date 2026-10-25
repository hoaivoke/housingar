import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from threading import Event

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

from housing_segmentation.app import create_app
from housing_segmentation.config import Settings


def jpeg(color=(180, 180, 180), size=(96, 64), orientation=None):
    image = Image.new("RGB", size, color)
    for x in range(20, 60):
        for y in range(15, 45):
            image.putpixel((x, y), (25, 80, 140))
    output = BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    image.save(output, "JPEG", quality=95, exif=exif)
    return output.getvalue()


@pytest.fixture
def metadata():
    return {
        "session_id": "session-test", "timestamp_ns": 1234567890,
        "intrinsics": {"fx": 80.0, "fy": 80.0, "cx": 48.0, "cy": 32.0},
        "camera_pose_world": [0, 0, 0, 0, 0, 0, 1],
        "sensor_to_display_rotation_deg": 90, "depth_timestamp_ns": 1234567880,
    }


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, backend="floodfill"))
    with TestClient(app) as connection:
        yield connection


def submit(client, metadata, *, points=None, content=None, frame="frame-1"):
    return client.post("/v1/segment", data={
        "frame_id": frame, "points": json.dumps(points if points is not None else [[35, 30, 1]]),
        "metadata": json.dumps(metadata),
    }, files={"image": ("frame.jpg", content if content is not None else jpeg(), "image/jpeg")})


def test_health_reports_actual_fallback(client):
    assert client.get("/health").json() == {
        "status": "ok", "backend": "floodfill", "model": "opencv-floodfill", "device": "cpu",
    }


def test_multimasks_and_persisted_frame(client, metadata, tmp_path):
    response = submit(client, metadata)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"frame_id", "backend", "model", "width", "height", "embedding_cached", "masks", "timing_ms"}
    assert body["frame_id"] == "frame-1"
    assert body["backend"] == "floodfill" and body["model"] == "opencv-floodfill"
    assert (body["width"], body["height"]) == (96, 64)
    assert body["embedding_cached"] is False
    assert len(body["masks"]) == 3
    assert [m["score"] for m in body["masks"]] == sorted([m["score"] for m in body["masks"]], reverse=True)
    assert set(body["timing_ms"]) == {"decode", "embed", "predict", "postprocess", "total"}
    assert all(value >= 0 for value in body["timing_ms"].values())
    for mask in body["masks"]:
        decoded = Image.open(BytesIO(base64.b64decode(mask["mask_png_b64"])))
        pixels = np.array(decoded)
        assert decoded.size == (96, 64) and decoded.mode == "L"
        assert set(np.unique(pixels)) <= {0, 255}
        assert pixels[30, 35] == 255 and pixels[0, 0] == 0
        assert mask["area_fraction"] == np.count_nonzero(pixels) / pixels.size
        x0, y0, x1, y1 = mask["bbox"]
        assert 0 <= x0 < x1 <= 96 and 0 <= y0 < y1 <= 64
        assert len(mask["polygon"]) >= 3
        assert all(0 <= x < 96 and 0 <= y < 64 for x, y in mask["polygon"])
    folder, = (tmp_path / "frames" / "session-test").iterdir()
    assert (folder / "image.jpg").read_bytes() == jpeg()
    assert len(list(folder.glob("mask_*.png"))) == 3
    assert json.loads((folder / "metadata.json").read_text())["metadata"] == metadata
    assert json.loads((folder / "response.json").read_text()) == body


def test_repeat_corrections_reuse_cache_across_intervening_frame(client, metadata, tmp_path, monkeypatch):
    embed_calls = []
    backend = client.app.state.engine.backend
    original = backend.embed
    def count_embed(rgb):
        embed_calls.append(rgb.shape)
        return original(rgb)
    monkeypatch.setattr(backend, "embed", count_embed)
    assert submit(client, metadata).json()["embedding_cached"] is False
    assert submit(client, metadata, frame="other-frame").json()["embedding_cached"] is False
    corrected = submit(client, metadata, points=[[35, 30, 1], [1, 1, 0]])
    assert corrected.status_code == 200
    assert corrected.json()["embedding_cached"] is True
    assert corrected.json()["timing_ms"]["embed"] == 0
    assert len(embed_calls) == 2
    assert len(list((tmp_path / "frames" / "session-test").iterdir())) == 3


def test_cache_is_session_scoped_and_bounded(client, metadata):
    submit(client, metadata)
    other = dict(metadata, session_id="another-session")
    assert submit(client, other).json()["embedding_cached"] is False
    submit(client, metadata, frame="newest")
    assert submit(client, metadata).json()["embedding_cached"] is False


def test_reused_frame_id_with_different_pixels_is_conflict(client, metadata):
    submit(client, metadata)
    response = submit(client, metadata, content=jpeg((10, 10, 10)))
    assert response.status_code == 409
    assert "different image bytes" in response.json()["detail"]


def test_exif_orientation_is_not_applied(client, metadata):
    response = submit(client, metadata, content=jpeg(orientation=6))
    assert response.status_code == 200
    assert (response.json()["width"], response.json()["height"]) == (96, 64)


def test_negative_prompt_excludes_selected_region(client, metadata):
    response = submit(client, metadata, points=[[35, 30, 1], [35, 30, 0]])
    assert response.status_code == 200
    for mask in response.json()["masks"]:
        assert mask["area_fraction"] == 0
        assert mask["bbox"] == [0, 0, 0, 0] and mask["polygon"] == []


@pytest.mark.parametrize("points", [
    [], {}, [[96, 0, 1]], [[0, 64, 1]], [[-1, 2, 1]], [[1, 1, 2]], [[1, 1]],
    [[True, 1, 1]], [["one", 2, 1]], [[1, 1, "1"]], [[1, 1, 1]] * 129,
])
def test_invalid_points_return_4xx(client, metadata, points):
    response = submit(client, metadata, points=points)
    assert response.status_code == 422, response.text
    assert "points" in response.json()["detail"] or "point" in response.json()["detail"]


@pytest.mark.parametrize("field,value", [("points", "["), ("metadata", "{"), ("points", "[[NaN, 1, 1]]"), ("points", "[[1e999, 1, 1]]")])
def test_invalid_json(client, metadata, field, value):
    data = {"frame_id": "x", "points": "[[1,1,1]]", "metadata": json.dumps(metadata)}
    data[field] = value
    response = client.post("/v1/segment", data=data, files={"image": ("x.jpg", jpeg())})
    assert response.status_code == 400
    assert field in response.json()["detail"]


@pytest.mark.parametrize("field,value", [
    ("session_id", "../escape"), ("timestamp_ns", -1), ("timestamp_ns", True),
    ("intrinsics", {"fx": 0, "fy": 1, "cx": 0, "cy": 0}),
    ("camera_pose_world", [0, 0]), ("sensor_to_display_rotation_deg", 45),
    ("depth_timestamp_ns", "yesterday"),
])
def test_invalid_metadata(client, metadata, field, value):
    metadata[field] = value
    response = submit(client, metadata)
    assert response.status_code == 422, response.text


def test_missing_image(client, metadata):
    response = client.post("/v1/segment", data={"frame_id": "x", "points": "[[1,1,1]]", "metadata": json.dumps(metadata)})
    assert response.status_code == 422


def test_unsupported_image_type(client, metadata):
    stream = BytesIO()
    Image.new("RGB", (96, 64)).save(stream, "GIF")
    assert submit(client, metadata, content=stream.getvalue()).status_code == 415


def test_missing_metadata_field(client, metadata):
    metadata.pop("intrinsics")
    assert submit(client, metadata).status_code == 422


def test_empty_frame_id(client, metadata):
    assert submit(client, metadata, frame="").status_code == 422


@pytest.mark.parametrize("content,status", [(b"", 400), (b"not an image", 400), (b"x" * (10 * 1024 * 1024 + 1), 413)])
def test_invalid_image(client, metadata, content, status):
    assert submit(client, metadata, content=content).status_code == status


def test_large_pixel_dimensions_rejected(client, metadata):
    buffer = BytesIO()
    Image.new("RGB", (3000, 3000)).save(buffer, "PNG")
    assert submit(client, metadata, content=buffer.getvalue()).status_code == 413


def test_logs_roundtrip_and_append(client, tmp_path):
    assert client.get("/v1/logs").json() == {"diagnostics": [], "drift": [], "geometry": []}
    payload = {"session_id": "s1", "device": "Pixel 9a", "nested": {"depth": True}}
    diagnostic = client.post("/v1/diagnostics", json=payload)
    assert diagnostic.status_code == 200
    assert diagnostic.json()["status"] == "saved"
    path = tmp_path / diagnostic.json()["path"]
    assert path.parent.name == "diagnostics" and path.name.endswith("_s1.json")
    assert json.loads(path.read_text()) == payload
    for drift in (1.2, 2.3):
        response = client.post("/v1/logs/drift", json={"session_id": "s1", "drift_cm": drift})
        assert response.status_code == 200
        assert response.json()["path"] == "drift/s1.jsonl"
    saved = [json.loads(line) for line in (tmp_path / "drift/s1.jsonl").read_text().splitlines()]
    assert [entry["drift_cm"] for entry in saved] == [1.2, 2.3]
    listing = client.get("/v1/logs").json()
    assert len(listing["diagnostics"]) == len(listing["drift"]) == 1
    assert listing["drift"][0]["size_bytes"] > 0
    assert listing["diagnostics"][0]["path"] == diagnostic.json()["path"]


@pytest.mark.parametrize("route", ["/v1/diagnostics", "/v1/logs/drift"])
@pytest.mark.parametrize("payload", [{}, {"session_id": "../../bad"}, [], "bad"])
def test_log_input_errors(client, route, payload):
    assert 400 <= client.post(route, json=payload).status_code < 500


@pytest.mark.parametrize("route", ["/v1/diagnostics", "/v1/logs/drift"])
def test_log_bad_json(client, route):
    assert client.post(route, content="{", headers={"content-type": "application/json"}).status_code == 422
    assert client.post(route, content='{"session_id":"s","n":NaN}', headers={"content-type": "application/json"}).status_code == 422


def test_health_stays_responsive_during_inference(client, metadata, monkeypatch):
    entered, release = Event(), Event()
    backend = client.app.state.engine.backend
    original = backend.predict
    def blocking_predict(*args):
        entered.set()
        assert release.wait(5), "test did not release inference"
        return original(*args)
    monkeypatch.setattr(backend, "predict", blocking_predict)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(submit, client, metadata)
        assert entered.wait(5)
        try:
            assert client.get("/health").status_code == 200
            assert not pending.done()
        finally:
            release.set()
        assert pending.result().status_code == 200
