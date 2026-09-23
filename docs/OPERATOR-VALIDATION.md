# Operator Cutover Runbook (v0.9.15 → v1.0.0)

v1.0.0 moves the scheduler off pyscript and runs it natively inside this
integration. The automated suite (`bash tools/test.sh` + `tests_brain/`,
including engine unit tests, differential tests against the legacy pyscript
app, and HA integration tests) already proves the engine matches the old
app's behavior. What's left is a real-box checklist for cutting over an
existing 0.9.x install without losing calibration history or watering the
lawn unattended.

This is a **runbook to follow on the operator's box**, not a CI gate — it
can't run in CI (needs real Rachio hardware and a live pyscript install to
migrate from).

---

## Before (~10 min)

1. Snapshot a baseline over read-only SSH: `irrigation_efficacy.json` and the
   current `last_nightly` record from `/config/pyscript/geodrops_rachio_state/`
   (or from `pyscript.geodrops_rachio_last_nightly`'s attributes).
2. Grep the box's live `automations.yaml` (and any dashboards) for
   `pyscript.geodrops_rachio_*` references.
3. Open a config-repo PR repointing them at the new entities:
   - `pyscript.geodrops_rachio_last_nightly` → `sensor.geodrops_rachio_last_nightly`
     (same attribute names).
   - `pyscript.geodrops_rachio_status` → `sensor.geodrops_rachio_status`.
     Check each reference: a comparison against a lowercase status token
     (`waiting`, `running`, `idle`, ...) needs to become
     `state_attr('sensor.geodrops_rachio_status', 'status')` instead of a
     state comparison.

   Merge this PR **right after** the upgrade below, once the new entities
   exist.

## Upgrade (daytime, 10:00–20:00 — never mid-run)

1. HACS → Download v1.0.0.
2. Restart Home Assistant.
3. Pull the dashboard/automations PR from step 3 above.

## Day-one checks (~15 min)

1. **Migration ran.** The log shows a migration summary (zones imported,
   files removed, pyscript reloaded or not), and
   `/config/pyscript/geodrops_rachio.py` no longer exists.
2. **Calibration carried over.** Each zone's Calibration State / efficacy
   sensor matches the pre-upgrade snapshot.
3. **Preview works.** Press *Preview irrigation plan* →
   `sensor.geodrops_rachio_plan` lists the same zones yesterday's plan did,
   with minutes that look plausible for today's moisture.
4. **Dashboards render.** The Lawn Ops dashboard (or equivalent) shows no
   missing-entity errors after the PR from step 3 is merged.

## First night (supervised)

- **23:00** — a plan is published and `sensor.geodrops_rachio_status`'s
  `status` attribute reads `waiting`.
- **Morning** — `sensor.geodrops_rachio_last_run` shows delivered ≈ planned,
  no `aborted_reason`, and the Rachio API call count is in the usual range.
- **After 09:00** — the settle-and-learn pass accepts/rejects observations
  normally (compare against the pre-upgrade pattern).

## Rollback (~5 min)

1. HACS → Redownload → pick v0.9.15.
2. Restart Home Assistant. (Setup re-delivers the pyscript script, which
   reads the untouched `/config/pyscript/geodrops_rachio_state/` directory
   the native integration left in place.)
3. Revert the dashboard/automations PR from step 3 above.

**Cost:** any calibration learned since the upgrade is lost — v0.9.15 has no
way to see it.

---

## Notes

- The unload safety stop (v1.0.0): if Home Assistant restarts or reloads
  this integration while valves are watering, it now stops the controller
  and its zones instead of leaving Rachio's own paused-schedule auto-resume
  to run the rest of the night unattended. Nothing to verify on a normal
  cutover, but worth knowing if you restart HA mid-run for any reason.
- **Known issue, unchanged from 0.9.x:** a Home Assistant restart during the
  pre-dawn wait for the watering window does not re-arm that night's run.
  Pre-existing bug; a fix is planned. Don't restart HA during the wait if you
  need that night's run to happen.
- `/config/pyscript/geodrops_rachio_state/` is left in place after the
  upgrade specifically so rollback works; don't delete it until you're
  confident you won't need to roll back.
