#!/bin/bash
set -euo pipefail
reconstruction_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$reconstruction_root/.build/modules"
xcrun swiftc -swift-version 5 -parse-as-library -target "$(uname -m)-apple-macosx14.0" \
  -module-cache-path "$reconstruction_root/.build/modules" \
  "$reconstruction_root/ObjectCapture.swift" -o "$reconstruction_root/.build/object-capture"
echo "$reconstruction_root/.build/object-capture"
