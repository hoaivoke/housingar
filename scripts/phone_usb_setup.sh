#!/bin/bash
# USB forwarding and exported reports for com.housingar.inspect. Bash 3.2 compatible.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adb_bin="${ADB:-}"
if [ -z "$adb_bin" ]; then
  adb_bin="$(command -v adb || true)"
fi
if [ -z "$adb_bin" ] && [ -x "${ANDROID_HOME:-$HOME/Library/Android/sdk}/platform-tools/adb" ]; then
  adb_bin="${ANDROID_HOME:-$HOME/Library/Android/sdk}/platform-tools/adb"
fi
if [ -z "$adb_bin" ] || ! command -v "$adb_bin" >/dev/null 2>&1; then
  echo "FAIL: adb is missing. Install Android SDK Platform Tools or set ADB to its executable." >&2
  exit 1
fi
if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [path/to/app.apk | pull-logs]" >&2
  exit 2
fi
if [ "$#" -eq 1 ] && [ "$1" = "--help" ]; then
  echo "Usage: $0 [path/to/app.apk | pull-logs]"
  exit 0
fi
device_output="$("$adb_bin" devices)"
printf '%s\n' "$device_output"
device_lines="$(printf '%s\n' "$device_output" | awk 'NR > 1 && NF >= 2 {print $1, $2}')"
device_count="$(printf '%s\n' "$device_lines" | awk 'NF {n++} END {print n+0}')"
if [ "$device_count" -ne 1 ]; then
  echo "FAIL: expected exactly one attached device; found $device_count. Connect only the Pixel and enable USB debugging." >&2
  exit 1
fi
serial="$(printf '%s\n' "$device_lines" | awk '{print $1}')"
device_state="$(printf '%s\n' "$device_lines" | awk '{print $2}')"
if [ "$device_state" != "device" ]; then
  echo "FAIL: $serial is $device_state. Unlock the phone and accept its USB debugging prompt." >&2
  exit 1
fi

if [ "${1:-}" = "pull-logs" ]; then
  safe_serial="$(printf '%s' "$serial" | tr -c 'A-Za-z0-9_-' '_')"
  destination="$repo_root/segmentation-service/data/phone/$(date -u +%Y%m%dT%H%M%SZ)_$safe_serial"
  mkdir -p "$destination"
  missing=0
  for kind in reports drift; do
    remote="/sdcard/Android/data/com.housingar.inspect/files/$kind"
    if "$adb_bin" -s "$serial" shell test -d "$remote"; then
      "$adb_bin" -s "$serial" pull "$remote" "$destination/"
    else
      echo "FAIL: phone directory $remote is unavailable. Save a report/drift run in the app first." >&2
      missing=1
    fi
  done
  echo "Downloaded available logs to: $destination"
  exit "$missing"
fi

if [ "$#" -eq 1 ] && [ ! -f "$1" ]; then
  echo "FAIL: APK does not exist: $1" >&2
  exit 1
fi
"$adb_bin" -s "$serial" reverse tcp:8000 tcp:8000
echo "PASS: Pixel http://127.0.0.1:8000 forwards to the Mac service on port 8000."
if [ "$#" -eq 1 ]; then
  "$adb_bin" -s "$serial" install -r "$1"
  echo "PASS: APK installed."
fi
echo "Start the Mac service, then open com.housingar.inspect on the phone."
