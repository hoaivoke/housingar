#!/bin/bash
set -euo pipefail
capture_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$capture_root/.build/modules"
xcrun swiftc -swift-version 5 -parse-as-library \
  -target "$(uname -m)-apple-macosx13.0" \
  -module-cache-path "$capture_root/.build/modules" \
  "$capture_root/Sources/CaptureCLI.swift" \
  -o "$capture_root/.build/housing-object-capture"
echo "Built: $capture_root/.build/housing-object-capture"
