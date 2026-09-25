#!/usr/bin/env bash
# Publish the version in custom_components/*/manifest.json as a GitHub release
# (which is what HACS delivers). Run on main after the release PR is merged.
#
#   X.Y.Z-beta.N  -> pre-release: HACS offers it only to users who turned on
#                    this integration's HACS "Pre-release" switch.
#   X.Y.Z         -> stable release, marked latest: every HACS user gets it.
#
# The release notes are the CHANGELOG.md section for the version ("## vX.Y.Z"
# or "## X.Y.Z", optionally followed by " — Title"), so the changelog and the
# release body cannot drift.
#
# Usage: tools/release.sh publish [--dry-run]
#   --dry-run  run every check and show the notes, but tag and publish nothing.
set -euo pipefail

usage() { echo "usage: $0 publish [--dry-run]" >&2; exit 2; }
[ "${1:-}" = "publish" ] || usage
DRY_RUN=0
case "${2:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  *) usage ;;
esac

cd "$(git rev-parse --show-toplevel)"

problems=()
problem() { problems+=("$1"); echo "  FAIL  $1"; }
ok() { echo "  ok    $1"; }

# --- Version -----------------------------------------------------------------
shopt -s nullglob
manifests=(custom_components/*/manifest.json)
if [ "${#manifests[@]}" -ne 1 ]; then
  echo "error: expected exactly one custom_components/*/manifest.json, found ${#manifests[@]}" >&2
  exit 1
fi
MANIFEST="${manifests[0]}"
VERSION=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST")
TAG="v$VERSION"
if [[ "$VERSION" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-beta\.[0-9]+$ ]]; then
  PRERELEASE=true
  BASE_TAG="v${BASH_REMATCH[1]}"
elif [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  PRERELEASE=false
else
  echo "error: $MANIFEST version \"$VERSION\" is neither X.Y.Z nor X.Y.Z-beta.N" >&2
  exit 1
fi
if [ "$PRERELEASE" = true ]; then
  echo "Publishing $TAG as a BETA (pre-release) from $MANIFEST"
else
  echo "Publishing $TAG as STABLE (every HACS user gets it) from $MANIFEST"
fi
echo

# --- Repository state --------------------------------------------------------
echo "Checks:"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = main ]; then ok "on main"; else problem "on branch '$BRANCH', not main"; fi

if [ -z "$(git status --porcelain)" ]; then ok "working tree clean"; else problem "working tree has uncommitted changes"; fi

git fetch --quiet origin main --tags
SHA=$(git rev-parse HEAD)
if [ "$SHA" = "$(git rev-parse origin/main)" ]; then
  ok "HEAD matches origin/main (${SHA:0:7})"
else
  problem "HEAD (${SHA:0:7}) is not origin/main ($(git rev-parse --short origin/main)); pull or push first"
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || [ -n "$(git ls-remote --tags origin "refs/tags/$TAG")" ]; then
  problem "tag $TAG already exists; bump $MANIFEST"
else
  ok "tag $TAG is new"
fi

if [ "$PRERELEASE" = true ]; then
  if git rev-parse -q --verify "refs/tags/$BASE_TAG" >/dev/null; then
    problem "$BASE_TAG is already released stable; a beta of it would sort below it"
  else
    ok "$BASE_TAG not released yet"
  fi
fi

# CI: the latest run of every workflow on HEAD must have passed.
ci=$(gh run list --commit "$SHA" --json workflowName,status,conclusion \
  --jq 'unique_by(.workflowName)[] | "\(.workflowName)|\(.status)|\(.conclusion)"' 2>/dev/null || true)
# unique_by keeps the first entry per workflow; gh lists newest first.
if [ -z "$ci" ]; then
  problem "no CI runs found for ${SHA:0:7}"
else
  while IFS='|' read -r name status conclusion; do
    # inside .github/workflows/publish.yml, that run is itself in progress
    if [ -n "${GITHUB_WORKFLOW:-}" ] && [ "$name" = "$GITHUB_WORKFLOW" ]; then
      continue
    fi
    if [ "$status" != completed ]; then
      problem "CI '$name' is still $status"
    elif [ "$conclusion" != success ]; then
      problem "CI '$name' concluded $conclusion"
    else
      ok "CI '$name' passed"
    fi
  done <<< "$ci"
fi

# --- Release notes from CHANGELOG.md -----------------------------------------
HEADING=$(awk -v ver="$VERSION" '
  /^## / { t = $2; sub(/^v/, "", t); if (t == ver) { print; exit } }
' CHANGELOG.md)
NOTES=$(awk -v ver="$VERSION" '
  /^## / { if (found) exit; t = $2; sub(/^v/, "", t); if (t == ver) { found = 1; next } }
  found { print }
' CHANGELOG.md | sed -e '/./,$!d')
if [ -z "$HEADING" ]; then
  problem "CHANGELOG.md has no '## $TAG' section"
elif [ -z "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ]; then
  problem "CHANGELOG.md's '$HEADING' section is empty"
else
  ok "notes from CHANGELOG.md '$HEADING'"
fi

TITLE="$TAG"
case "$HEADING" in
  *" — "*) TITLE="$TAG — ${HEADING#* — }" ;;
esac
if [ "$PRERELEASE" = true ]; then
  NOTES="> **Beta.** HACS only offers this to users who turned on this integration's *Pre-release* switch in HACS.

$NOTES"
fi

echo
echo "Title: $TITLE"
echo "----- notes -----"
printf '%s\n' "$NOTES"
echo "-----------------"
echo

if [ "${#problems[@]}" -gt 0 ]; then
  echo "Not publishing: ${#problems[@]} check(s) failed." >&2
  exit 1
fi

FLAG=--latest
[ "$PRERELEASE" = true ] && FLAG=--prerelease

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run: all checks passed. Would run:"
  echo "  git tag $TAG && git push origin $TAG"
  echo "  gh release create $TAG --verify-tag $FLAG --title \"$TITLE\" --notes-file <notes>"
  exit 0
fi

NOTES_FILE=$(mktemp)
trap 'rm -f "$NOTES_FILE"' EXIT
printf '%s\n' "$NOTES" > "$NOTES_FILE"

git tag "$TAG"
git push origin "$TAG"
gh release create "$TAG" --verify-tag "$FLAG" --title "$TITLE" --notes-file "$NOTES_FILE"
bash tools/check_release.sh "$TAG" "$PRERELEASE"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  # a release made with a workflow's token doesn't trigger release-guard.yml;
  # check_release.sh just above is the check
  echo "Published $TAG."
else
  echo "Published $TAG. release-guard.yml re-checks it on GitHub."
fi
