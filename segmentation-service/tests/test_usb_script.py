import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def usb(tmp_path):
    scripts = tmp_path / "repo/scripts"
    scripts.mkdir(parents=True)
    original = Path(__file__).resolve().parents[2] / "scripts/phone_usb_setup.sh"
    script = scripts / original.name
    shutil.copy(original, script)
    log = tmp_path / "calls.jsonl"
    adb = tmp_path / "fake-adb"
    adb.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["ADB_TEST_LOG"], "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args == ["devices"]:
    print("List of devices attached\\n" + os.environ.get("ADB_TEST_DEVICES", "PIXEL123\\tdevice"))
elif args[2:4] == ["shell", "test"]:
    sys.exit(1 if os.environ.get("ADB_TEST_MISSING") == "1" else 0)
elif args[2:3] == ["pull"]:
    target = pathlib.Path(args[4]) / pathlib.PurePosixPath(args[3]).name
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text("{}")
''')
    adb.chmod(0o755)
    env = dict(os.environ, ADB=str(adb), ADB_TEST_LOG=str(log))
    def run(*args, **extra):
        result = subprocess.run(["/bin/bash", str(script), *map(str, args)], env=dict(env, **extra), text=True, capture_output=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run, tmp_path


def test_usb_setup_and_install_with_spaces(usb):
    run, folder = usb
    apk = folder / "my app.apk"
    apk.write_bytes(b"test")
    result, calls = run(apk)
    assert result.returncode == 0, result.stderr
    assert ["-s", "PIXEL123", "reverse", "tcp:8000", "tcp:8000"] in calls
    assert ["-s", "PIXEL123", "install", "-r", str(apk)] in calls


@pytest.mark.parametrize("devices", ["", "PIXEL123\tunauthorized", "PIXEL123\toffline", "ONE\tdevice\nTWO\tdevice"])
def test_usb_rejects_ambiguous_or_unready_device(usb, devices):
    run, _ = usb
    result, calls = run(ADB_TEST_DEVICES=devices)
    assert result.returncode == 1
    assert len(calls) == 1


def test_usb_pull_logs(usb):
    run, folder = usb
    result, calls = run("pull-logs")
    assert result.returncode == 0, result.stderr
    assert len(list((folder / "repo/segmentation-service/data/phone").rglob("report.json"))) == 2
    assert not any("reverse" in call for call in calls)


def test_usb_missing_logs_report_failure(usb):
    run, _ = usb
    result, _ = run("pull-logs", ADB_TEST_MISSING="1")
    assert result.returncode == 1
    assert "unavailable" in result.stderr
