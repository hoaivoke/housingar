#!/bin/bash
# Read-only readiness checks. Bash 3.2 compatible; exits nonzero if any check fails.
set -uo pipefail
passes=0
failures=0
pass() { printf 'PASS: %s\n' "$*"; passes=$((passes + 1)); }
fail() { printf 'FAIL: %s\n' "$*"; failures=$((failures + 1)); }

if [ "$(uname -s)" != "Darwin" ]; then
  fail "macOS is required for this readiness check"
  exit 1
fi
if os_version="$(sw_vers -productVersion 2>/dev/null)"; then pass "macOS $os_version"; else fail "cannot read macOS version"; fi
if chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"; then pass "Chip: $chip ($(uname -m))"; else fail "cannot read chip (possibly sandbox restricted)"; fi
if ram_bytes="$(sysctl -n hw.memsize 2>/dev/null)"; then
  ram_gib="$(awk -v bytes="$ram_bytes" 'BEGIN {printf "%.1f", bytes/1073741824}')"
  pass "RAM: $ram_gib GiB"
else fail "cannot read RAM (possibly sandbox restricted)"; fi
if disk="$(df -h . | awk 'NR == 2 {print $4}')" && [ -n "$disk" ]; then pass "Free disk: $disk"; else fail "cannot read free disk"; fi

if command -v python3 >/dev/null 2>&1; then
  python_version="$(python3 --version 2>&1)"
  printf '%s\n' "python3: $(command -v python3) ($python_version)"
  if python3 -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)'; then
    pass "python3 is supported (3.10–3.12)"
  else fail "default python3 is outside 3.10–3.12; activate the service venv"; fi
else fail "python3 is missing"; fi
if command -v node >/dev/null 2>&1; then pass "Node: $(node --version)"; else fail "Node is missing"; fi
if java_home="$(/usr/libexec/java_home 2>/dev/null)" && [ -x "$java_home/bin/java" ]; then
  pass "Java: $("$java_home/bin/java" -version 2>&1 | head -n 1)"
else fail "a Java runtime was not found by java_home"; fi
studio_path=""
for candidate in "/Applications/Android Studio.app" "$HOME/Applications/Android Studio.app"; do
  if [ -d "$candidate" ]; then studio_path="$candidate"; break; fi
done
if [ -n "$studio_path" ]; then pass "Android Studio: $studio_path"; else fail "Android Studio is missing from Applications"; fi

adb_bin="${ADB:-$(command -v adb || true)}"
if [ -z "$adb_bin" ] && [ -x "${ANDROID_HOME:-$HOME/Library/Android/sdk}/platform-tools/adb" ]; then
  adb_bin="${ANDROID_HOME:-$HOME/Library/Android/sdk}/platform-tools/adb"
fi
if [ -n "$adb_bin" ] && command -v "$adb_bin" >/dev/null 2>&1; then
  pass "adb: $adb_bin"
  if devices="$("$adb_bin" devices 2>&1)"; then
    printf '%s\n' "$devices"
    attached="$(printf '%s\n' "$devices" | awk '$2 == "device" {n++} END {print n+0}')"
    if [ "$attached" -eq 1 ]; then pass "one authorized device is attached"; else fail "expected one authorized device; found $attached"; fi
  else fail "adb devices failed: $devices"; fi
else fail "adb is missing (install Android SDK Platform Tools)"; fi

temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/housing-ar-check.XXXXXX")" || exit 1
trap 'rm -rf "$temp_dir"' EXIT
if clt_path="$(xcode-select -p 2>/dev/null)"; then pass "Xcode developer tools: $clt_path"; else fail "Xcode Command Line Tools are missing"; fi
if swift_version="$(xcrun swift --version 2>&1)"; then
  pass "Swift: $(printf '%s\n' "$swift_version" | head -n 1)"
  cat > "$temp_dir/probe.swift" <<'SWIFT'
import RealityKit
if #available(macOS 12.0, *) {
    print(PhotogrammetrySession.isSupported)
} else {
    print(false)
}
SWIFT
  if xcrun swiftc -module-cache-path "$temp_dir/modules" "$temp_dir/probe.swift" -o "$temp_dir/probe" >"$temp_dir/swift.log" 2>&1; then
    if supported="$("$temp_dir/probe" 2>"$temp_dir/probe.log")" && [ "$supported" = "true" ]; then
      pass "PhotogrammetrySession.isSupported = true"
    else
      fail "Object Capture support probe returned ${supported:-an execution error}"
      cat "$temp_dir/probe.log"
    fi
  else
    fail "could not compile the RealityKit Object Capture support probe"
    cat "$temp_dir/swift.log"
  fi
else fail "Swift is unavailable: $swift_version"; fi

if command -v lsof >/dev/null 2>&1 && listeners="$(lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null)" && [ -n "$listeners" ]; then
  pass "Port 8000 has a listener"
  printf '%s\n' "$listeners"
else fail "no visible listener on port 8000 (start the segmentation service)"; fi
if curl --fail --silent --show-error --connect-timeout 2 --max-time 4 http://127.0.0.1:8000/health >"$temp_dir/health.json"; then
  if python3 - "$temp_dir/health.json" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    health = json.load(stream)
print(json.dumps(health))
assert health.get("status") == "ok"
assert health.get("backend") in ("sam2", "floodfill")
assert health.get("device") in ("mps", "cpu")
assert isinstance(health.get("model"), str)
PY
  then pass "GET /health returned the expected service contract"; else fail "GET /health returned an unexpected response"; fi
else fail "GET http://127.0.0.1:8000/health failed"; fi

printf '\nSummary: %s PASS, %s FAIL\n' "$passes" "$failures"
[ "$failures" -eq 0 ]
