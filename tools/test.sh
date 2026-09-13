#!/usr/bin/env bash
# Run the test suite inside the cached HA test image. Windows cannot run the
# Home Assistant test harness natively (it imports the Unix-only `fcntl`), so
# HA-coupled tests run in this Linux container. Pure-logic tests also run here.
set -euo pipefail
IMAGE="geodrops-test"
HOSTDIR="$(pwd -W 2>/dev/null || pwd)"
MSYS_NO_PATHCONV=1 exec docker run --rm -v "${HOSTDIR}:/app" -w /app "$IMAGE" pytest "$@"
