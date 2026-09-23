# Releasing

This repo contains the whole Home Assistant integration — the config flow,
entities, and the scheduler itself ("the brain") — as a single native
Python component under `custom_components/geodrops_rachio/`. As of v1.0.0
there is no pyscript delivery: the scheduler runs natively inside the
integration, split into `brain/` (pure logic — planning, calibration,
drought, weather, dosing) and `engine/` (the HA-coupled runtime — the
scheduler, store, orchestration, and I/O). Both are edited directly, in this
repo.

`brain/`'s unit tests live in `tests_brain/`; the rest of the suite —
including differential tests against the legacy pyscript app — lives in
`tests/` (see below).

There is only **one version stamp** now: `custom_components/geodrops_rachio/manifest.json`'s
`"version"`. Bump it on every release — HACS uses it (together with the git
tag/release) to know a new version exists at all.

**Every release needs a Home Assistant restart.** Because the scheduler runs
as part of the integration's own Python, HA must re-import the whole
component on every update — there is no "brain-only, no restart" path
anymore (that was a pyscript-delivery mechanism, removed in v1.0.0). Don't
write release notes implying otherwise; every CHANGELOG entry and release
body should assume a restart is required.

## Release steps

1. **Make your changes.** Edit `custom_components/geodrops_rachio/brain/`
   and/or `custom_components/geodrops_rachio/engine/` (or any other wrapper
   Python — config flow, entities, etc).

2. **Run the tests.**

   - **Brain (pure logic), natively on any platform:**

     ```bash
     PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain
     ```

     `tests_brain/` is the scheduler brain's own unit suite; its `conftest.py`
     puts the integration directory on the path so the tests import `brain`
     directly. No HA stack required.

   - **Everything else (HA-coupled), in the Linux test image:**

     ```bash
     bash tools/test.sh
     ```

     `pytest-homeassistant-custom-component` cannot run natively on Windows (it
     imports the Unix-only `fcntl`), so this suite runs inside a Linux Docker
     container. Build the image once (or after `Dockerfile.test` /
     `requirements_test.txt` change):

     ```bash
     docker build -f Dockerfile.test -t geodrops-test .
     ```

     `tools/test.sh` runs `pytest` in that container (arguments are
     forwarded), which collects **both** `tests/` and `tests_brain/` —
     including `tests/engine/`'s differential tests, which run the ported
     engine and the verbatim legacy pyscript app (`tests/legacy/geodrops_rachio_legacy.py`)
     against the same fixtures and assert identical behavior. CI
     (`.github/workflows/ci.yml`) runs the same suite on `ubuntu-latest`.

   Both suites must pass before releasing.

3. **Bump the version.** Edit
   `custom_components/geodrops_rachio/manifest.json` and increment
   `"version"`. Do this on every release.

4. **Commit.**

   ```bash
   git add custom_components/geodrops_rachio and any other changed files
   git commit -m "release: vX.Y.Z"
   ```

5. **Write the release notes.** HACS shows the GitHub release **body** as the
   changelog for that version, so it is how users see what a pending update
   contains before they install it — always write real notes, never
   `--generate-notes`. Keep them short and user-facing: what changed and what
   got fixed. Every release requires a restart — say so. Add the same summary
   as a new top section in `CHANGELOG.md`.

6. **Tag and release** from `main`.

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   gh release create vX.Y.Z --title vX.Y.Z --notes-file <notes.md>
   ```

   HACS installs updates from GitHub releases, so the release is what makes the
   new version — and its notes — visible to users' HACS instances; the
   `manifest.json` bump alone does not distribute anything. Because `hacs.json`
   sets `hide_default_branch`, HACS only ever offers tagged releases, not raw
   `main`.

## The legacy oracle

`tests/legacy/geodrops_rachio_legacy.py` is the verbatim v0.9.15 pyscript
app, kept only as the differential-test oracle (`tests/engine/` runs it
side by side with the native engine against identical fixtures and asserts
the same Rachio/notify/calendar/logbook calls, status transitions, and
persisted state). It is not shipped, not imported by the integration, and
not otherwise maintained.

**Keep it until the differential tests are replaced.** Most of the engine's
coverage comes from them: without them `orchestration.py` falls from ~89% to
~14% line coverage, `scheduler.py` to ~49% and `planning.py` to ~61%. Before
deleting the oracle, freeze each scenario's legacy outputs into JSON fixtures
and point the tests at those. The native-only tests (`test_runner.py`,
`test_learning.py`, `test_lifecycle.py`, `test_waiting_marker.py`, ...) do not
depend on the oracle.

## Sanity check before tagging

- `custom_components/geodrops_rachio/manifest.json`'s `"version"` was bumped.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain` passes.
- `bash tools/test.sh` passes.
- The release notes and CHANGELOG entry say a restart is required (every
  release needs one now — never claim otherwise).
