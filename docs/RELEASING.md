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
including the engine's golden-fixture scenario tests — lives in `tests/`
(see below).

There is only **one version stamp** now: `custom_components/geodrops_rachio/manifest.json`'s
`"version"`. Bump it on every release — HACS uses it (together with the git
tag/release) to know a new version exists at all.

**Every release needs a Home Assistant restart.** Because the scheduler runs
as part of the integration's own Python, HA must re-import the whole
component on every update — there is no "brain-only, no restart" path
anymore (that was a pyscript-delivery mechanism, removed in v1.0.0). Don't
write release notes implying otherwise; every CHANGELOG entry and release
body should assume a restart is required.

## Beta and stable

Releases go out on two channels, both as GitHub releases:

- **Beta — `X.Y.Z-beta.N`**, published as a GitHub *pre-release*. HACS offers
  it only to users who turned on this integration's **Pre-release** switch —
  since HACS 2.0 a per-repository switch entity, disabled by default (see the
  README's *Beta versions*). You run it on your own box first.
- **Stable — `X.Y.Z`**, the release every HACS user is offered.

**By default, merged changes ship as a beta.** Fixes found while a beta is out
go into the next beta (`-beta.2`, `-beta.3`, ...), not into a string of stable
patches. Promote to stable when you judge the beta ready — typically once it
has run through the nights that exercise the change. Nothing enforces the
wait, so the discipline is yours. A direct stable (no beta) is still allowed
for an urgent fix to a bug in the current stable.

Version numbers: betas carry the version the stable will get — the first beta
after v1.1.0 is `1.2.0-beta.1` (or `1.1.1-beta.1` for fixes only), and
promoting it is `1.2.0`. Never publish a beta of a version that is already
stable; it would sort below it (`tools/release.sh` refuses).

CHANGELOG sections:

- **Each beta** gets its own section, `## v1.2.0-beta.2 — Title`, covering
  what changed since the previous beta. Beta users read it as that release's
  notes.
- **The stable** gets `## v1.2.0 — Title` summarizing *everything* since the
  previous stable, written for users who skipped every beta. Don't just point
  at the beta sections. The beta sections stay in the file.

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
     including `tests/engine/`'s scenario tests, which run the engine through
     whole nights, runs and restarts and compare every effect with a golden
     fixture (see below). CI (`.github/workflows/ci.yml`) runs the same suite
     on `ubuntu-latest`.

     On Linux, skip Docker and run `pytest -q` directly under **Python 3.14**
     after `pip install -r requirements_test.txt`. Check that pip resolved the
     same Home Assistant as CI (`python -c "import homeassistant.const as c;
     print(c.__version__)"`): Python 3.13, and 3.14 release candidates, quietly
     get an older HA, below the `hacs.json` minimum.

   Both suites must pass before releasing.

3. **Open a release PR** (`release: vX.Y.Z`) with two changes:

   - **Bump the version** in `custom_components/geodrops_rachio/manifest.json`:
     the next beta (`1.2.0-beta.1`, `1.2.0-beta.2`, ...) or the stable
     (`1.2.0`). See *Beta and stable* above.
   - **Write the release notes** as a new top section in `CHANGELOG.md`,
     headed `## vX.Y.Z — Title`. `tools/release.sh` publishes that section
     verbatim as the GitHub release body, and HACS shows the body as the
     changelog for that version — it is how users see what a pending update
     contains before they install it. Keep it short and user-facing: what
     changed and what got fixed. Every release requires a restart — say so.

4. **Merge it**, then publish from an up-to-date `main`:

   ```bash
   git checkout main && git pull
   bash tools/release.sh publish --dry-run
   bash tools/release.sh publish
   ```

   The script refuses unless you are on a clean `main` that matches
   `origin/main`, CI passed on that commit, the tag is new, and `CHANGELOG.md`
   has the version's section. It then tags `vX.Y.Z` and creates the GitHub
   release — a pre-release for `-beta.N`, the latest release for a stable —
   titled from the CHANGELOG heading. `--dry-run` runs every check and shows
   the notes without publishing.

   HACS installs updates from GitHub releases, so the release is what makes the
   new version — and its notes — visible to users' HACS instances; the
   `manifest.json` bump alone does not distribute anything. Because `hacs.json`
   sets `hide_default_branch`, HACS only ever offers tagged releases, not raw
   `main`.

**Release guard.** `.github/workflows/release-guard.yml` re-checks every
published or edited release — including ones made by hand in the GitHub UI —
with `tools/check_release.sh`: the tag must equal `v` + the manifest version at
that tag, and the pre-release flag must match the version. On a mismatch it
turns the release back into a draft (HACS stops offering it) and the run
fails. Fix the cause, then publish the draft again.

## Golden fixtures

`tests/engine/golden/` holds the expected effects of each engine scenario —
every Rachio/notify/calendar/logbook call, the status transitions, the
published records, the persisted docs and the log trail — as JSON. The
`*_scenarios.py` tests and `test_scheduler.py` run the engine and compare its
effects with those fixtures (`tests/engine/golden.py`).

The fixtures were captured from the verbatim v0.9.15 pyscript app, which the
tests used to run side by side with the engine as a "legacy oracle". The oracle
has since been deleted; its source is still at the `v0.9.15` tag
(`custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py`), and the
engine modules' "Ported from" line numbers refer to it. So a passing scenario
still means "behaves exactly like v0.9.15" — until a fixture is regenerated.

**A deliberate behaviour change** (watering, calibration, logging, anything a
scenario observes) fails the affected scenarios, naming the part that moved
(`calls`, `records`, `logs`, ...). After confirming the new behaviour is the one
you want, regenerate and review the fixture diff as part of the change:

```bash
GOLDEN_UPDATE=1 pytest tests/engine -q
git diff tests/engine/golden
```

Only the scenarios you meant to change should move. Each scenario also asserts
that it still reaches the branch it is named for (`_assert_branch` and
friends), so a regeneration cannot quietly turn, say, the rain-abort scenario
into a normal night. The engine tests run with the process timezone pinned to
UTC (`tests/engine/conftest.py`), so fixtures are identical on any machine.

## Sanity check before publishing

- Beta or stable? Default to a beta; a stable should promote a beta that has
  already run on your box, unless it's an urgent fix.
- `custom_components/geodrops_rachio/manifest.json`'s `"version"` was bumped.
- A stable's CHANGELOG section covers everything since the previous stable,
  not just the last beta.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain` passes.
- `bash tools/test.sh` passes.
- The release notes and CHANGELOG entry say a restart is required (every
  release needs one now — never claim otherwise).
