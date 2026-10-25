import hashlib
import json
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / ".build/housing-object-capture"
PREPARE = ROOT / "prepare_capture.py"


def image(seed, size=(160, 120)):
    return Image.fromarray(np.random.default_rng(seed).integers(0, 256, (size[1], size[0], 3), dtype=np.uint8))


@pytest.fixture
def photo_folder(tmp_path):
    source = tmp_path / "source photos"
    source.mkdir()
    for index in range(10):
        image(index).save(source / f"photo-{index:02}.jpg", quality=95)
    return source


def swift(*arguments):
    assert BINARY.is_file(), "Build object-capture/build.sh before running these tests"
    return subprocess.run([str(BINARY), *map(str, arguments)], text=True, capture_output=True, timeout=15)


def prepare(source, output, *arguments):
    return subprocess.run([sys.executable, str(PREPARE), str(source), "--output", str(output), *map(str, arguments)],
                          text=True, capture_output=True, timeout=30)


def test_native_help():
    result = swift("--help")
    assert result.returncode == 0
    assert "reconstruct" in result.stdout and "not automatically rescaled" in result.stdout


def test_native_validation_does_not_reconstruct(photo_folder):
    result = swift("validate", "--input", photo_folder)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["event"] == "validated" and record["photo_count"] == 10
    assert record["reconstruction_attempted"] is False
    assert len(record["photos"]) == 10 and record["photos"][0]["width"] == 160


@pytest.mark.parametrize("args,part", [
    (("unknown",), "Unknown command"),
    (("validate",), "--input"),
    (("reconstruct", "--input", "/tmp"), "--output"),
    (("validate", "--input"), "Missing value"),
    (("validate", "--detail", "max"), "detail"),
    (("validate", "--width-m", "nan"), "finite"),
    (("validate", "--width-m", "-1"), "positive"),
    (("validate", "--width-m", "1"), "together"),
    (("validate", "--timeout-seconds", "999999999999999999"), "at most"),
    (("validate", "--input", "/tmp", "--input", "/tmp"), "Repeated option"),
    (("validate", "--surprise", "x"), "Unknown option"),
])
def test_native_bad_arguments(args, part):
    result = swift(*args)
    assert result.returncode == 1
    assert part in json.loads(result.stderr)["error"]


def test_native_rejects_too_few_photos(tmp_path):
    result = swift("validate", "--input", tmp_path)
    assert result.returncode == 1
    assert "At least 10" in result.stderr


def test_native_rejects_broken_photo(photo_folder):
    (photo_folder / "photo-00.jpg").write_bytes(b"broken")
    result = swift("validate", "--input", photo_folder)
    assert result.returncode == 1 and "Cannot read photo" in result.stderr


def test_native_refuses_existing_output_before_starting_capture(photo_folder, tmp_path):
    output = tmp_path / "existing.usdz"
    output.write_bytes(b"preserve this")
    result = swift("reconstruct", "--input", photo_folder, "--output", output)
    assert result.returncode == 1 and "Refusing to overwrite" in result.stderr
    assert output.read_bytes() == b"preserve this"
    assert not list(tmp_path.glob("*.capture.json"))


def test_native_refuses_writing_into_source(photo_folder):
    result = swift("reconstruct", "--input", photo_folder, "--output", photo_folder / "nested/model.usdz")
    assert result.returncode == 1 and "outside the source" in result.stderr


def test_native_refuses_output_symlink_to_source(photo_folder, tmp_path):
    alias = tmp_path / "source-alias"
    alias.symlink_to(photo_folder, target_is_directory=True)
    result = swift("reconstruct", "--input", photo_folder, "--output", alias / "new/model.usdz")
    assert result.returncode == 1 and "outside the source" in result.stderr
    assert not (photo_folder / "new").exists()


def test_prepare_photos_filters_and_preserves_sources(photo_folder, tmp_path):
    original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in photo_folder.iterdir()}
    (photo_folder / "duplicate.jpg").write_bytes((photo_folder / "photo-00.jpg").read_bytes())
    Image.new("RGB", (160, 120), "gray").save(photo_folder / "flat.jpg")
    (photo_folder / "broken.jpg").write_bytes(b"broken")
    output = tmp_path / "prepared"
    result = prepare(photo_folder, output)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "prepared" and manifest["accepted_count"] == 10
    assert manifest["rejected_count"] == 3
    assert manifest["scale_status"] == "unverified" and manifest["photogrammetry_attempted"] is False
    assert len(list(output.glob("*.jpg"))) == 10
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in photo_folder.iterdir() if p.name in original} == original
    native = swift("validate", "--input", output)
    assert native.returncode == 0, native.stderr


def test_prepare_refuses_overwrite(photo_folder, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    result = prepare(photo_folder, output)
    assert result.returncode == 1 and "already exists" in result.stderr
    assert (output / "keep.txt").read_text() == "keep"


def test_prepare_refuses_input_subfolder(photo_folder):
    result = prepare(photo_folder, photo_folder / "nested")
    assert result.returncode == 1 and "outside" in result.stderr
    assert not (photo_folder / "nested").exists()


def test_prepare_reports_insufficient_capture(tmp_path):
    source = tmp_path / "flat"
    source.mkdir()
    for index in range(10):
        Image.new("RGB", (160, 120), "gray").save(source / f"{index}.jpg")
    output = tmp_path / "failed"
    result = prepare(source, output)
    assert result.returncode == 1
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["accepted_count"] == 0


def test_prepare_video_and_explicit_rotation(tmp_path):
    video = tmp_path / "synthetic.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 8, (160, 120))
    assert writer.isOpened(), "MJPG video encoder is required for this video path test"
    for index in range(16):
        writer.write(cv2.cvtColor(np.asarray(image(index)), cv2.COLOR_RGB2BGR))
    writer.release()
    output = tmp_path / "video-frames"
    result = prepare(video, output, "--sample-every", "0.25", "--max-frames", "8", "--min-frames", "3", "--rotate", "90")
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["input_kind"] == "video" and manifest["accepted_count"] == 8
    assert [frame["video_frame"] for frame in manifest["accepted"]] == list(range(0, 16, 2))
    assert all((frame["width"], frame["height"]) == (120, 160) for frame in manifest["accepted"])
    assert Image.open(output / "frame_00000.jpg").size == (120, 160)


def test_prepare_applies_photo_exif_orientation(tmp_path):
    source = tmp_path / "exif"
    source.mkdir()
    for index in range(3):
        exif = Image.Exif()
        exif[274] = 6
        image(index, (96, 64)).save(source / f"{index}.jpg", exif=exif)
    output = tmp_path / "upright"
    result = prepare(source, output, "--min-frames", "3")
    assert result.returncode == 0, result.stderr
    assert Image.open(output / "frame_00000.jpg").size == (64, 96)


@pytest.mark.parametrize("args", [("--sample-every", "nan"), ("--sample-every", "0"), ("--max-frames", "1"), ("--max-side", "0")])
def test_prepare_rejects_invalid_options(photo_folder, tmp_path, args):
    result = prepare(photo_folder, tmp_path / "not-created", *args)
    assert result.returncode == 2
    assert not (tmp_path / "not-created").exists()
