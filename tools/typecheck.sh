#!/usr/bin/env bash
# Type-check the integration with mypy (configured in mypy.ini) inside the
# same Linux image as tools/test.sh. Arguments are forwarded to mypy.
set -euo pipefail
IMAGE="geodrops-rachio-test"
docker build -q -t "$IMAGE" - < Dockerfile.test > /dev/null
HOSTDIR="$(pwd -W 2>/dev/null || pwd)"
MSYS_NO_PATHCONV=1 exec docker run --rm -v "${HOSTDIR}:/app" -w /app "$IMAGE" \
  mypy custom_components/geodrops_rachio "$@"
