"""Private single-job subprocess entry point used by the persistent queue."""
import json
from pathlib import Path
import sys
import traceback

from .jobs import atomic_json
from .pipeline import PipelineOptions, process_capture


def main():
    inputs, work = Path(sys.argv[1]), Path(sys.argv[2])
    options = PipelineOptions(**json.loads(sys.argv[3]))

    def update(state, progress, message):
        atomic_json(work / "worker-progress.json", {"state": state, "progress": progress, "message": message})

    try:
        process_capture(inputs, work, update, lambda: None, options=options)
    except BaseException as exc:
        atomic_json(work / "worker-error.json", {"error": str(exc), "type": type(exc).__name__})
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
