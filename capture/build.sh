#!/bin/bash
# Builds the Tadween audio capture helper: capture/TadweenCapture.swift -> capture/tadween-capture
# Needs only the Command Line Tools (swiftc); works from any working directory.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# -swift-version 5 avoids Swift 6 strict-concurrency errors. macOS 13 is the minimum for
# ScreenCaptureKit audio capture; newer APIs are guarded with #available.
swiftc -O -swift-version 5 \
    -target "$(uname -m)-apple-macos13.0" \
    -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia \
    TadweenCapture.swift -o tadween-capture

echo "Built $(pwd)/tadween-capture"
