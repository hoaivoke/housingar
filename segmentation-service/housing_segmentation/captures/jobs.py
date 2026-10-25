from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import queue
import shutil
import tempfile
from threading import Event, RLock, Thread
from uuid import uuid4

from ..engine import InputError
from ..storage import timestamp
from .format import MAX_ARCHIVE_BYTES, extract_archive, parse_manifest
from .pipeline import PipelineOptions
from .isolation import run_isolated_capture

TERMINAL = {"done", "failed", "cancelled"}
STATES = {"queued", "validating", "masking", "reconstructing_object", "reconstructing_background", "texturing", "exporting", 'fusing_scene','finding_objects','segmenting_objects','meshing_objects', *TERMINAL}


class JobCancelled(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class JobManager:
    def __init__(self, root, *, runner=None, options=None, inference_lock=None, scan_runner=None, scan_options=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._file_lock = (self.root / ".worker.lock").open("a")
        try:
            fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file_lock.close()
            raise RuntimeError("Another capture worker is using this data directory; run one service worker") from exc
        self._lock = RLock()
        self._jobs = {}
        self._events = {}
        self._queue = queue.Queue()
        self._closing = Event()
        self.runner = runner or run_isolated_capture
        self.options = options or PipelineOptions.from_env()
        self.inference_lock = inference_lock
        from ..scans.pipeline import ScanOptions
        from .isolation import run_isolated_scan
        self.scan_root = self.root.parent / 'scans'
        self.scan_root.mkdir(exist_ok=True)
        self.scan_runner = scan_runner or run_isolated_scan
        self.scan_options = scan_options or ScanOptions.from_env()
        for folder in [*self.root.iterdir(),*self.scan_root.iterdir()]:
            if not folder.is_dir() or folder.is_symlink() or not (folder / "job.json").is_file():
                continue
            try:
                job = json.loads((folder / "job.json").read_text())
                if job.get('scan_id',job.get('capture_id')) != folder.name:
                    continue
                if job["state"] not in TERMINAL:
                    job.update(state="failed", message="Interrupted by service restart", error="Service stopped before this capture completed", outputs={'proposals':None} if job.get('kind')=='scan' else {"object_glb": None, "background_glb": None, "report": None})
                    atomic_json(folder / "job.json", job)
                self._jobs[job["job_id"]] = job
                self._events[job["job_id"]] = Event()
            except (ValueError, KeyError, OSError):
                continue
        self._thread = Thread(target=self._worker, name="capture-reconstruction", daemon=True)
        self._thread.start()

    def ingest(self, payload, archive_stream):
        return self._ingest(payload,archive_stream,scan=False)

    def ingest_scan(self,payload,archive_stream):
        return self._ingest(payload,archive_stream,scan=True)

    def _ingest(self,payload,archive_stream,*,scan):
        from ..scans.format import parse_scan, MAX_SCAN_BYTES, MAX_SCAN_UNCOMPRESSED
        manifest = parse_scan(payload) if scan else parse_manifest(payload)
        root = self.scan_root if scan else self.root
        identifier = manifest.scan_id if scan else manifest.capture_id
        id_field = 'scan_id' if scan else 'capture_id'
        maximum = MAX_SCAN_BYTES if scan else MAX_ARCHIVE_BYTES
        uploads = root / ".uploads"
        uploads.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="capture-", dir=uploads) as temp:
            temporary = Path(temp)
            archive = temporary / "archive.zip"
            size = 0
            with archive.open("wb") as output:
                while data := archive_stream.read(1024 * 1024):
                    size += len(data)
                    if size > maximum:
                        raise InputError(f"Archive exceeds {maximum//1_000_000} MB", 413)
                    output.write(data)
            input_folder = temporary / "inputs"
            if scan:
                extract_archive(archive,input_folder,manifest,max_archive_bytes=maximum,max_uncompressed_bytes=MAX_SCAN_UNCOMPRESSED)
            else:
                extract_archive(archive, input_folder, manifest)
            (input_folder / "manifest.json").write_text(json.dumps(manifest.model_dump(by_alias=True), indent=2, allow_nan=False))
            with self._lock:
                folder = root / identifier
                if folder.exists():
                    raise InputError(f"{id_field} already exists; choose a new ID", 409)
                if self._closing.is_set():
                    raise InputError("Capture worker is stopping", 503)
                prepared = temporary / "ready"
                prepared.mkdir()
                input_folder.replace(prepared / "inputs")
                archive.replace(prepared / "archive.zip")
                job_id = uuid4().hex
                job = {id_field: identifier, 'kind':'scan' if scan else 'capture', "job_id": job_id, "state": "queued", "progress": 0.,
                       "message": "Queued", "error": None, "created_at": timestamp(),
                       "outputs": {'proposals':None} if scan else {"object_glb": None, "background_glb": None, "report": None}}
                atomic_json(prepared / "job.json", job)
                prepared.replace(folder)
                self._jobs[job_id] = job
                self._events[job_id] = Event()
                self._queue.put(job_id)
        return {id_field: identifier, "job_id": job_id}

    def folder(self,job):
        return self.scan_root/job['scan_id'] if job.get('kind')=='scan' else self.root/job['capture_id']

    def scan_job(self,scan_id):
        with self._lock:
            for job in self._jobs.values():
                if job.get('kind')=='scan' and job.get('scan_id')==scan_id:
                    return deepcopy(job)
        raise InputError('Unknown scan',404)

    def get(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                raise InputError("Unknown capture job", 404)
            return deepcopy(self._jobs[job_id])

    def _update(self, job_id, state, progress, message, error=None):
        with self._lock:
            job = self._jobs[job_id]
            if job["state"] in TERMINAL:
                return
            if state not in STATES or not 0 <= progress <= 1:
                raise ValueError("Invalid job state/progress")
            job.update(state=state, progress=max(job["progress"], float(progress)), message=message, error=error)
            if state == "done":
                if job.get('kind')=='scan':
                    job['outputs']={'proposals':f"/v1/scans/{job['scan_id']}/proposals"}
                else:
                    prefix = f"/v1/jobs/{job_id}/files"
                    job["outputs"] = {"object_glb": f"{prefix}/object.glb", "background_glb": f"{prefix}/background.glb", "report": f"{prefix}/report.json"}
            folder = self.folder(job)
            atomic_json(folder / "job.json", job)
            with (folder / "events.jsonl").open("a") as stream:
                stream.write(json.dumps({"timestamp": timestamp(), "state": state, "progress": job["progress"], "message": message, "error": error}) + "\n")

    def cancel(self, job_id):
        with self._lock:
            job = self.get(job_id)
            if job["state"] == "cancelled":
                return job
            if job["state"] in {"done", "failed"}:
                raise InputError("Job has already finished", 409)
            self._events[job_id].set()
            if job["state"] == "queued":
                self._update(job_id, "cancelled", job["progress"], "Cancelled before processing")
            else:
                self._update(job_id, job["state"], job["progress"], "Cancellation requested")
            return self.get(job_id)

    def output_file(self, job_id, name):
        if name not in {"object.glb", "background.glb", "report.json"}:
            raise InputError("Unknown output file", 404)
        job = self.get(job_id)
        if job["state"] != "done":
            raise InputError("Job outputs are not available until done", 409)
        if job.get('kind')=='scan':
            raise InputError('Use the scan proposal URLs',404)
        path = self.folder(job) / "outputs" / name
        if not path.is_file() or path.is_symlink():
            raise InputError("Output file is missing", 404)
        return path

    def _worker(self):
        try:
            while not self._closing.is_set():
                job_id = self._queue.get()
                if job_id is None:
                    break
                job = self.get(job_id)
                if job["state"] in TERMINAL:
                    continue
                def check():
                    if self._closing.is_set() or self._events[job_id].is_set():
                        raise JobCancelled("Capture processing cancelled")
                def update(state, progress, message):
                    check()
                    self._update(job_id, state, progress, message)
                try:
                    check()
                    self._update(job_id, "validating", .01, "Starting scan worker" if job.get('kind')=='scan' else "Starting capture worker")
                    check()
                    folder = self.folder(job)
                    scan=job.get('kind')=='scan'
                    runner=self.scan_runner if scan else self.runner
                    runner(folder / "inputs", folder, update, check, options=self.scan_options if scan else self.options, inference_lock=self.inference_lock)
                    check()
                    self._update(job_id, "done", 1., "Object proposals ready for review" if scan else "Metric object mesh exported")
                except JobCancelled as exc:
                    if self._closing.is_set():
                        self._update(job_id, "failed", self.get(job_id)["progress"], "Interrupted by service shutdown", "Service stopped before this capture completed")
                    else:
                        self._update(job_id, "cancelled", self.get(job_id)["progress"], str(exc))
                except Exception as exc:
                    self._update(job_id, "failed", self.get(job_id)["progress"], "Scan reconstruction failed" if job.get('kind')=='scan' else "Capture reconstruction failed", str(exc))
        finally:
            self._file_lock.close()

    def close(self):
        self._closing.set()
        for event in self._events.values():
            event.set()
        self._queue.put(None)
        self._thread.join(timeout=15)
