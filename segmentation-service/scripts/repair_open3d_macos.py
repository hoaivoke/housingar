#!/usr/bin/env python3
"""Repair Open3D's missing Homebrew libusb link inside this venv only."""
import importlib.util
from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != "darwin":
        return
    spec = importlib.util.find_spec("open3d")
    if spec is None:
        raise SystemExit("Install requirements-reconstruction.txt first")
    from libusb_package import get_library_path
    library = Path(get_library_path()).resolve()
    root = Path(spec.origin).parent
    changed = []
    for binary in root.glob("libOpen3D*.dylib"):
        linked = subprocess.check_output(["otool", "-L", str(binary)], text=True)
        for line in linked.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if "libusb" in dependency and dependency.startswith("/opt/homebrew/") and not Path(dependency).exists():
                subprocess.run(["install_name_tool", "-change", dependency, str(library), str(binary)], check=True)
                subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True)
                changed.append(str(binary))
    print({"changed_venv_libraries": changed, "local_libusb": str(library)})


if __name__ == "__main__":
    main()
