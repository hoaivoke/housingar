import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import Lock
from uuid import uuid4

from .engine import InputError


def validate_session_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise InputError("session_id must contain 1–128 letters, digits, underscores or hyphens")
    return value


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


class Storage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._append_lock = Lock()
        for name in ("frames", "diagnostics", "drift", "geometry"):
            self._path(name).mkdir(parents=True, exist_ok=True)

    def _path(self, *parts):
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root):
            raise InputError("storage path would leave the configured data directory")
        return path

    def _relative(self, path):
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def _json(payload):
        try:
            return json.dumps(payload, allow_nan=False, ensure_ascii=False)
        except (ValueError, TypeError) as exc:
            raise InputError("JSON must contain serializable finite values") from exc

    def save_frame(self, image, metadata, points, result):
        session = validate_session_id(metadata["session_id"])
        folder = self._path("frames", session, f"{timestamp()}_{uuid4().hex[:8]}")
        folder.mkdir(parents=True)
        extension = "jpg" if image.startswith(b"\xff\xd8") else "png"
        (folder / f"image.{extension}").write_bytes(image)
        (folder / "metadata.json").write_text(self._json({
            "frame_id": result["frame_id"], "points": points, "metadata": metadata,
        }), encoding="utf-8")
        (folder / "response.json").write_text(self._json(result), encoding="utf-8")
        for index, mask in enumerate(result["masks"]):
            (folder / f"mask_{index}.png").write_bytes(base64.b64decode(mask["mask_png_b64"]))

    def save_diagnostics(self, payload):
        session = validate_session_id(payload.get("session_id"))
        serialized = self._json(payload)
        path = self._path("diagnostics", f"{timestamp()}_{session}.json")
        # Exclusive creation avoids overwriting even under simultaneous submissions.
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        return {"status": "saved", "path": self._relative(path)}

    def append_drift(self, payload):
        session = validate_session_id(payload.get("session_id"))
        serialized = self._json(payload)
        path = self._path("drift", f"{session}.jsonl")
        with self._append_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        return {"status": "saved", "path": self._relative(path)}

    def save_geometry(self, payload):
        session = validate_session_id(payload.get("session_id"))
        selection = validate_session_id(payload.get("selection_id"))
        serialized = self._json(payload)
        path = self._path("geometry", f"{session}_{selection}.json")
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        temporary.replace(path)
        return {"status": "saved", "path": self._relative(path)}

    def list_logs(self):
        result = {}
        for kind, pattern in (("diagnostics", "*.json"), ("drift", "*.jsonl"), ("geometry", "*.json")):
            result[kind] = []
            for path in sorted(self._path(kind).glob(pattern), reverse=True):
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                result[kind].append({
                    "path": self._relative(path),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                })
        return result
