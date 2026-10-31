#!/usr/bin/env bash
# Publish the version in custom_components/*/manifest.json as a GitHub release
# (which is what HACS delivers). Run on main after the release PR is merged,
# locally or through .github/workflows/publish.yml.
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
#
# Environment:
#   RELEASE_CI_TIMEOUT  seconds to wait for CI runs on HEAD to start and
#                       finish (default 900).
set -euo pipefail

# Workflows that must have a passing push run on HEAD. HACS wants the release
# created after its checks (Validate) passed, and right after a merge they may
# not have started yet.
REQUIRED_WORKFLOWS=("CI" "Validate")
CI_TIMEOUT="${RELEASE_CI_TIMEOUT:-900}"

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
if ! command -v gh >/dev/null; then
  problem "gh is not installed"
elif ! gh auth status >/dev/null 2>&1; then
  problem "gh is not signed in; run gh auth login"
else
  ok "gh signed in"
fi

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
  # HACS doesn't sort releases: it offers the first pre-release in GitHub's
  # release list, and GitHub lists beta.9 ahead of beta.10. A beta.10 would
  # publish fine and never be offered.
  if [ "${VERSION##*-beta.}" -ge 10 ]; then
    problem "beta number ${VERSION##*-beta.} is above 9 and HACS won't offer it; promote ${BASE_TAG#v} or move to the next patch's -beta.1"
  else
    ok "beta number is single-digit"
  fi
fi

# --- CI on HEAD --------------------------------------------------------------
# Only push runs count: publish.yml dry runs (workflow_dispatch) and
# release-guard.yml (release) also run on HEAD, and an earlier failed dry run
# must not block publishing. The latest push run of every workflow must pass,
# and every REQUIRED_WORKFLOWS entry must have one. Runs not started or not
# finished yet (CI on a just-merged release PR) are waited for, up to
# CI_TIMEOUT, so publishing right after the merge needs no separate wait.
ci_runs() {
  # unique_by keeps the first entry per workflow; gh lists newest first.
  gh run list --commit "$SHA" --event push --json databaseId,workflowName,status,conclusion \
    --jq 'unique_by(.workflowName)[] | "\(.databaseId)|\(.workflowName)|\(.status)|\(.conclusion)"' \
    2>/dev/null || true
}
missing_workflows() {
  local required
  for required in "${REQUIRED_WORKFLOWS[@]}"; do
    grep -q "^[^|]*|$required|" <<< "$ci" || echo "$required"
  done
}
# Polled rather than followed with `gh run watch`, so one deadline also covers
# a run stuck in the queue. Each wait is printed once.
shown=""
note() { grep -qxF -- "$1" <<< "$shown" || { echo "  wait  $1"; shown+="$1"$'\n'; }; }
deadline=$((SECONDS + CI_TIMEOUT))
while :; do
  ci=$(ci_runs)
  pending=$(awk -F'|' '$3 != "completed"' <<< "$ci")
  missing=$(missing_workflows)
  [ -z "$pending$missing" ] && break
  [ "$SECONDS" -ge "$deadline" ] && break
  while IFS='|' read -r _ name status _; do
    [ -n "$name" ] && note "CI '$name' is $status; waiting for it to finish"
  done <<< "$pending"
  while read -r name; do
    [ -n "$name" ] && note "CI '$name' has no run on ${SHA:0:7} yet; waiting for it to start"
  done <<< "$missing"
  sleep 15
done
while read -r name; do
  [ -n "$name" ] && problem "CI '$name' has no push run on ${SHA:0:7} after ${CI_TIMEOUT}s"
done <<< "$missing"
if [ -n "$ci" ]; then
  while IFS='|' read -r _ name status conclusion; do
    if [ "$status" != completed ]; then
      problem "CI '$name' is still $status after ${CI_TIMEOUT}s"
    elif [ "$conclusion" != success ]; then
      problem "CI '$name' concluded $conclusion"
    else
      ok "CI '$name' passed"
    fi
  done <<< "$ci"
fi

# --- Release notes from CHANGELOG.md -----------------------------------------
# CR is dropped: a core.autocrlf checkout holds CHANGELOG.md with CRLF. Read
# once into a variable: piping a large file into an awk that exits early
# kills the writer with SIGPIPE, which pipefail turns into a silent exit.
CHANGELOG=$(tr -d '\r' < CHANGELOG.md)
HEADING=$(awk -v ver="$VERSION" '
  /^## / { t = $2; sub(/^v/, "", t); if (t == ver) { print; exit } }
' <<< "$CHANGELOG")
NOTES=$(awk -v ver="$VERSION" '
  /^## / { if (found) exit; t = $2; sub(/^v/, "", t); if (t == ver) { found = 1; next } }
  found { print }
' <<< "$CHANGELOG" | sed -e '/./,$!d')
if [ -z "$HEADING" ]; then
  problem "CHANGELOG.md has no '## $TAG' section"
elif [ -z "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ]; then
  problem "CHANGELOG.md's '$HEADING' section is empty"
else
  ok "notes from CHANGELOG.md '$HEADING'"
fi

# HACS heads each release's notes "<tag> - <title>" when the two differ, so
# the title is the heading's subtitle alone; without one it is the tag.
TITLE="$TAG"
case "$HEADING" in
  *" — "*) TITLE="${HEADING#* — }" ;;
esac
TITLE=$(printf '%s' "$TITLE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
[ -n "$TITLE" ] || TITLE="$TAG"
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
release_exists() { # retried: create just failed, so the network may be flaky
  local try
  for try in 1 2 3; do
    gh release view "$TAG" >/dev/null 2>&1 && return 0
    if [ "$try" -lt 3 ]; then sleep 3; fi
  done
  return 1
}
if gh release create "$TAG" --verify-tag "$FLAG" --title "$TITLE" --notes-file "$NOTES_FILE"; then
  :
elif release_exists; then
  # gh failed client-side (e.g. a timeout) after GitHub created the release.
  echo "warning: gh release create reported an error, but release $TAG exists; checking it"
else
  # Left in place, the tag would block a rerun ("tag already exists").
  if git push --quiet --delete origin "$TAG" && git tag -d "$TAG" >/dev/null; then
    echo "error: creating the release failed; removed tag $TAG again, so a rerun can retry" >&2
  else
    echo "error: creating the release failed and tag $TAG is still there; remove it: git push --delete origin $TAG && git tag -d $TAG" >&2
  fi
  exit 1
fi
bash tools/check_release.sh "$TAG" "$PRERELEASE"

# HACS takes the first non-draft release of each kind (pre-release, stable)
# from GitHub's list, so the new tag has to come first there or HACS users
# won't be offered it. The list can lag a just-created release; retry briefly.
first=""
for try in 1 2 3 4 5 6; do
  first=$(gh api "repos/{owner}/{repo}/releases?per_page=20" \
    --jq "[.[] | select(.draft | not) | select(.prerelease == $PRERELEASE)][0].tag_name" || true)
  [ "$first" = "$TAG" ] && break
  if [ "$try" -lt 6 ]; then sleep 5; fi
done
if [ -z "$first" ] || [ "$first" = null ]; then
  echo "error: $TAG is published, but the script could not read GitHub's release list to check that HACS offers it." >&2
  echo "Check by hand: gh api \"repos/{owner}/{repo}/releases?per_page=3\" -q '.[].tag_name'" >&2
  exit 1
fi
if [ "$first" != "$TAG" ]; then
  echo "error: GitHub lists $first ahead of $TAG, so HACS won't offer $TAG; publish the next version instead" >&2
  exit 1
fi
echo "ok: $TAG is first in GitHub's release list, so HACS offers it"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  # a release made with a workflow's token doesn't trigger release-guard.yml;
  # check_release.sh just above is the check
  echo "Published $TAG."
else
  echo "Published $TAG. release-guard.yml re-checks it on GitHub."
fi
