"""Run native/GPU reconstruction outside the ASGI process and its Python GIL."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import sleep

from ..config import SERVICE_ROOT
from .pipeline import inference_guard


def stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_isolated_capture(input_folder, work, update, check, *, options, inference_lock=None):
    return _run_isolated(input_folder,work,update,check,options=options,inference_lock=inference_lock,module='housing_segmentation.captures.worker')


def run_isolated_scan(input_folder,work,update,check,*,options,inference_lock=None):
    return _run_isolated(input_folder,work,update,check,options=options,inference_lock=inference_lock,module='housing_segmentation.scans.worker')


def _run_isolated(input_folder, work, update, check, *, options, inference_lock, module):
    """One subprocess per job; the manager still serializes all queued captures."""
    work = Path(work).resolve()
    progress_path = work / "worker-progress.json"
    error_path = work / "worker-error.json"
    command = [sys.executable, "-m", module,
               str(Path(input_folder).resolve()), str(work), json.dumps(asdict(options))]
    # A process-local torch/native state avoids sharing model globals across the
    # ASGI thread pool. Serialize GPU use with interactive image segmentation.
    with inference_guard(inference_lock, check), (work / "worker.log").open("w") as log:
        check()
        process = subprocess.Popen(command, cwd=SERVICE_ROOT, start_new_session=True,
                                   stdout=log, stderr=subprocess.STDOUT)
        (work / "worker-pid.json").write_text(json.dumps({"pid": process.pid, "parent_pid": os.getpid()}))
        previous = None

        def forward_progress():
            nonlocal previous
            if progress_path.exists():
                payload = progress_path.read_text()
                if payload != previous:
                    item = json.loads(payload)
                    update(item["state"], item["progress"], item["message"])
                    previous = payload

        try:
            while process.poll() is None:
                check()
                forward_progress()
                sleep(.1)
            check()
            forward_progress()
            if process.returncode:
                error = json.loads(error_path.read_text())["error"] if error_path.exists() else f"Reconstruction subprocess exited {process.returncode}; inspect worker.log"
                raise RuntimeError(error)
            if not (work / "outputs/report.json").is_file():
                raise RuntimeError("Reconstruction worker exited without its report")
        except BaseException:
            # Swift Object Capture belongs to this same process group, so a
            # cancellation/shutdown cannot leave it running as an orphan.
            stop_process_group(process)
            raise
