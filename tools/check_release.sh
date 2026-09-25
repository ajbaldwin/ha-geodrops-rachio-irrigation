#!/usr/bin/env bash
# Check that a GitHub release is consistent before HACS users see it:
#   - the tag is "v" + the manifest.json version at that tag, and
#   - the pre-release flag matches the version (X.Y.Z-beta.N is a pre-release,
#     X.Y.Z is not). HACS only offers pre-releases to users who turned on
#     its per-repository "Pre-release" switch, so a wrong flag ships a beta to everyone.
#
# Usage: tools/check_release.sh <tag> <prerelease: true|false>
# Run by .github/workflows/release-guard.yml and by tools/release.sh.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <tag> <prerelease: true|false>" >&2
  exit 2
fi
TAG="$1"
PRERELEASE="$2"

cd "$(git rev-parse --show-toplevel)"

MANIFEST=$(git ls-tree -r --name-only "$TAG" -- custom_components | grep '/manifest\.json$' || true)
if [ "$(printf '%s\n' "$MANIFEST" | grep -c .)" -ne 1 ]; then
  echo "error: expected exactly one custom_components/*/manifest.json at $TAG, found: ${MANIFEST:-none}" >&2
  exit 1
fi
VERSION=$(git show "$TAG:$MANIFEST" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

fail=0
if [ "$TAG" != "v$VERSION" ]; then
  echo "error: tag $TAG does not match $MANIFEST version \"$VERSION\" (expected tag v$VERSION)" >&2
  fail=1
fi

if [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  want=false
elif [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+-beta\.[0-9]+$ ]]; then
  want=true
else
  echo "error: version \"$VERSION\" is neither X.Y.Z nor X.Y.Z-beta.N" >&2
  exit 1
fi
if [ "$PRERELEASE" != "$want" ]; then
  if [ "$want" = true ]; then
    echo "error: $VERSION is a beta but the release is not marked pre-release (every HACS user would get it)" >&2
  else
    echo "error: $VERSION is a stable version but the release is marked pre-release (only beta users would get it)" >&2
  fi
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "ok: $TAG matches $MANIFEST, pre-release=$PRERELEASE"
fi
exit "$fail"
