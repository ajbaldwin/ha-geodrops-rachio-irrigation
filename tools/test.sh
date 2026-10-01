#!/usr/bin/env bash
# Run the test suite inside the HA test image. Windows cannot run the
# Home Assistant test harness natively (it imports the Unix-only `fcntl`), so
# HA-coupled tests run in this Linux container. Pure-logic tests also run here.
#
# The image has its own tag and is (re)built here on every run: the GeoDrops
# repo's tools/test.sh builds `geodrops-test` from its own Python 3.13
# Dockerfile, and sharing that tag silently ran this suite on HA 2026.2.
# Dockerfile.test copies nothing, so it builds without a context (stdin) and
# a cached build takes about a second.
set -euo pipefail
IMAGE="geodrops-rachio-test"
docker build -q -t "$IMAGE" - < Dockerfile.test > /dev/null
HOSTDIR="$(pwd -W 2>/dev/null || pwd)"
MSYS_NO_PATHCONV=1 exec docker run --rm -v "${HOSTDIR}:/app" -w /app "$IMAGE" pytest "$@"
