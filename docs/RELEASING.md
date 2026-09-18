# Releasing

This repo contains **both** the Home Assistant wrapper (config flow, entities,
`delivery.py`, `updater.py`) **and** the scheduler itself ("the brain") in
`custom_components/geodrops_rachio/bundled_app/`. The brain used to be vendored
from a separate canonical repo; that repo was merged in and archived, so the
brain now lives in-tree and is **edited directly** here. Its pure-logic unit
tests live in `tests_brain/` (see below).

Every release has **two version stamps** — keep them straight, because they
answer different questions:

| Stamp | Where | What it versions | What changing it affects |
|---|---|---|---|
| `manifest.json` `"version"` | `custom_components/geodrops_rachio/manifest.json` | This wrapper's own Python (config flow, entities, `delivery.py`, `updater.py`, ...) | What HACS shows the user, and whether HACS/HA flags the update as needing a restart |
| `bundled_app/VERSION` | `custom_components/geodrops_rachio/bundled_app/VERSION` | An informational label for the brain bundle | Nothing functional — see the note below |

**`bundled_app/VERSION` is informational only.** `delivery.py` decides whether
to re-copy the pyscript script + `geodrops_rachio_lib` and `pyscript.reload`
by comparing a **content hash** of the bundle (`bundle_fingerprint`) against the
stamp on the box, *not* the VERSION string. So any real edit to `bundled_app/`
redelivers on the next config-entry setup/reload regardless of VERSION. Update
VERSION when you want a human-readable marker for the brain, but it is not
load-bearing.

A release that only changes `bundled_app/` (brain edit, no wrapper `.py` files
touched) is picked up by already-running installs **with no Home Assistant
restart**: `updater.py` listens on the HACS update entity, reloads the config
entry when it changes, and `delivery.py` re-hashes `bundled_app/`, sees it
differs from the stamp in the user's pyscript directory, copies the new script +
library, and calls `pyscript.reload` — all without HA re-importing any Python
module.

A release that changes any file under `custom_components/geodrops_rachio/`
*itself* (config flow, platforms, `delivery.py`, `updater.py`, etc.) needs a
**Home Assistant restart** after HACS installs it, same as any other custom
integration update.

**Messaging rule — do not advertise "no restart."** HACS shows a "restart Home
Assistant" prompt after *every* download, regardless of what changed: it
replaces the whole integration folder and sees the manifest bump, and cannot
tell a brain-only change from a wrapper-Python change. The brain-only auto-apply
above is real, but a user still sees the prompt — so a release note that claims
"no restart needed" only confuses. Never make that claim in release notes or the
CHANGELOG. Call out a restart **only** when wrapper Python changed (then it is
genuinely required); for a brain-only release, say nothing about restarts.

**Always bump `manifest.json`'s version for every release**, even a brain-only
change — HACS uses it (together with the git tag/release) to know a new version
exists at all.

## Release steps

1. **Make your changes.** Edit the brain directly under
   `custom_components/geodrops_rachio/bundled_app/` (`geodrops_rachio.py` +
   `geodrops_rachio_lib/`) and/or the wrapper Python. Optionally update
   `bundled_app/VERSION` as a human-readable brain marker.

2. **Run the tests.**

   - **Brain (pure logic), natively on any platform:**

     ```bash
     PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain
     ```

     `tests_brain/` is the scheduler brain's own unit suite; its `conftest.py`
     puts `bundled_app/` on the path so the tests import the delivered
     `geodrops_rachio_lib` directly. No HA stack required.

   - **Wrapper (HA-coupled), in the Linux test image:**

     ```bash
     bash tools/test.sh
     ```

     `pytest-homeassistant-custom-component` cannot run natively on Windows (it
     imports the Unix-only `fcntl`), so the wrapper tests run inside a Linux
     Docker container. Build the image once (or after `Dockerfile.test` /
     `requirements_test.txt` change):

     ```bash
     docker build -f Dockerfile.test -t geodrops-test .
     ```

     `tools/test.sh` runs `pytest` in that container (arguments are forwarded),
     which collects **both** `tests/` and `tests_brain/`. CI
     (`.github/workflows/ci.yml`) runs the same suite on `ubuntu-latest`.

3. **Bump the wrapper version.** Edit
   `custom_components/geodrops_rachio/manifest.json` and increment `"version"`.
   Do this on every release, per the table above.

4. **Commit.**

   ```bash
   git add custom_components/geodrops_rachio manifest and any other changed files
   git commit -m "release: vX.Y.Z"
   ```

5. **Write the release notes.** HACS shows the GitHub release **body** as the
   changelog for that version, so it is how users see what a pending update
   contains before they install it — always write real notes, never
   `--generate-notes`. Keep them short and user-facing: what changed and what got
   fixed. Mention a restart **only** when wrapper Python changed; never claim a
   release skips the restart (see the messaging rule above). Add the same summary
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

## Sanity check before tagging

- `custom_components/geodrops_rachio/manifest.json`'s `"version"` was bumped.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain` passes.
- `bash tools/test.sh` passes.
- If you touched any wrapper `.py` file in this release, mention in the release
  notes that it requires a Home Assistant restart after updating. For a
  brain-only change, say nothing about restarts — do NOT claim it needs no
  restart (HACS prompts one regardless; see the messaging rule above).
