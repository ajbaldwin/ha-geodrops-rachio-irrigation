# Native Engine (Remove pyscript) — Design

Date: 2026-09-22
Status: Draft — awaiting user review
Target release: v1.0.0 (breaking)
Branch: `feature/native-engine` (lands AFTER `feature/options-flow-hub-refactor`)

## Problem

The integration is a wrapper: it generates `geodrops_rachio_config.yaml`, copies
the scheduler (`bundled_app/geodrops_rachio.py`, 2,847 lines, plus
`geodrops_rachio_lib/`) into `/config/pyscript/`, and calls `pyscript.reload`.
Consequences:

- Users must install and configure pyscript (`allow_all_imports`) first.
- The app layer — the code that opens valves — cannot be unit-tested; it only
  runs inside pyscript.
- A second data path: the scheduler publishes `pyscript.geodrops_rachio_*`
  state entities and JSON files, and the coordinator mirrors them back into
  native sensors.
- Pyscript workarounds: 8 `@pyscript_compile` helpers, 26 `task.executor`
  calls, a vendored-script delivery/fingerprint/updater pipeline.

## Goals

1. Run the scheduler natively inside the integration. No pyscript dependency.
2. Port behaviour **unchanged** — no scheduling/calibration logic changes ride
   along in this release.
3. Make the valve-control layer testable.
4. One hard-cutover release (v1.0.0) with a documented rollback to v0.9.15.

## Non-goals

- Shadow/parallel-engine mode (rejected: plan-only shadowing never exercises the
  valve loops, and costs ~300–400 lines of throwaway code plus a second release).
- A compatibility mirror of `pyscript.geodrops_rachio_*` entities.
- New HA services (buttons cover every action; automations use `button.press`).
- Changes to `brain/` logic.

## Architecture

### Module layout (`custom_components/geodrops_rachio/`)

| Path | Role | Source |
|---|---|---|
| `brain/` | Pure logic, unchanged apart from relative imports | moved from `bundled_app/geodrops_rachio_lib/` |
| `engine/port.py` | `HAPort` protocol: `state`, `attr`, `call`, `sleep`, `now`, `notify`, `logbook`. `HassPort` (real) and `FakePort` (tests, simulated clock) | new |
| `engine/runner.py` | Valve loops: `run_plan`, `run_collapsed`, `_walk_segment`, `_sleep_watching`, block start/stop/pause/resume helpers | ported from app (~900 lines) |
| `engine/planning.py` | `_plan_context`, preview body, target floors, weather/sensor reads, rain checks | ported from app (~800 lines) |
| `engine/scheduler.py` | `Scheduler` class: trigger wiring, run lifecycle, settle-and-learn, startup recovery, record publishing | ported from app (~700 lines) |
| `engine/store.py` | `Store` wrapper + one-time legacy migration + pyscript retirement | new |
| `rachio_client.py` | Absorbs the app's `_fetch_zone_data` (runtimes/depths/spans); async via `async_get_clientsession` | existing, extended |

Deleted: `bundled_app/`, `delivery.py`, `updater.py`, the YAML-emitting half of
`config_writer.py`, and any `tools/` helpers that only served vendoring/delivery.

### Key decisions

- **Async throughout.** Every app function that touched `state`/`service`/`task`
  becomes `async def`. Rejected: running the sync code in an executor thread
  (holds an HA worker thread for hours; cancellation is unreliable).
- **Module globals → instance attributes** on `Scheduler` (`_run_in_progress`,
  `_watering_active`, `_rain_since`, counters, runtime cache, current cfg).
- **Config in memory.** `config_writer` returns a dict; the scheduler calls
  `brain.config.parse_config(dict)` directly. No YAML file.
- **Preview takes its config explicitly.** The `_preview()` save/restore of
  `_current_cfg`/`_current_bindings` (and the wake-during-preview race it guards)
  is removed.

## Runtime

### Trigger mapping

| pyscript | Native |
|---|---|
| `cron(0 23 * * *)` nightly | `async_track_time_change(hour=23, minute=0, second=0)` |
| `cron(0 6 * * *)` calibrate | `async_track_time_change(hour=6, minute=0, second=0)` |
| `cron(*/30 * * * *)` settle | `async_track_time_change(minute=[0, 30], second=0)` |
| `@time_trigger("startup")` + `task.sleep(30)` | `async_at_started`, then wait (bounded timeout) for bound Rachio switches to be available |
| `@state_trigger("button.geodrops_rachio_stop")` | `StopButton.async_press` → `scheduler.async_stop()` |
| 5 `@service` actions | removed; buttons call `Scheduler` methods |
| `task.executor` / `@pyscript_compile` | removed (async HTTP; Store for persistence) |

All unsubscribe callbacks go through `entry.async_on_unload`.

### Run lifecycle

- Exactly one `self._run_task`. `_start_run(coro)` cancels any existing run task
  and awaits its cancellation, then starts the new one with
  `entry.async_create_background_task` (replaces `task.unique("geodrops_rachio_run")`).
- Stop = cancel the run task. The existing `finally` blocks (stop valves, clear
  `run_active`, set status) run on `CancelledError`. Port audit item: no
  `except BaseException` / bare `except` may swallow `CancelledError`.
- Unload (reload, options change, shutdown) cancels the run task — same
  semantics as `pyscript.reload` today: a waiting run re-arms at startup from the
  waiting marker; an interrupted watering run is handled by the existing
  startup recovery (`run_active` marker + open-valve check).

## State and entities

### Storage

One `homeassistant.helpers.storage.Store` per entry, key
`geodrops_rachio.<entry_id>`, saved immediately (no delay) on every mutation:

- `efficacy`, `pending_obs` (calibration history — the valuable data)
- `records`: `last_nightly`, `calibration`, `targets`, `preview`
- `runtime_cache`, `waiting_marker`

### One-time migration + pyscript retirement

Runs on setup when the Store is empty:

1. Import legacy JSON from `/config/pyscript/geodrops_rachio_state/` (efficacy,
   pending obs, waiting marker, persisted records) into the Store.
2. Delete delivered files: `/config/pyscript/geodrops_rachio.py`,
   `/config/pyscript/modules/geodrops_rachio_lib/`,
   `/config/pyscript/geodrops_rachio_config.yaml`.
3. If the `pyscript.reload` service exists, call it so a legacy script already
   loaded this boot is torn down (its startup handler sleeps 30 s then may re-arm
   a waiting run — without this, both engines could water the same night).
4. Leave `geodrops_rachio_state/` in place (rollback needs it).
5. Log a summary: zones imported, files removed, pyscript reloaded or not.

`manifest.json`: drop `after_dependencies: ["pyscript"]`.

### Entities

| Before | After |
|---|---|
| `pyscript.geodrops_rachio_status` | existing `sensor.geodrops_rachio_status`, fed directly |
| `pyscript.geodrops_rachio_last_nightly` | new `sensor.geodrops_rachio_last_run` — state = run outcome; attributes = full record, **same attribute names** |
| `pyscript.geodrops_rachio_preview` | new `sensor.geodrops_rachio_plan` — same pattern |
| `pyscript.geodrops_rachio_{calibration,targets,runtimes}` | internal only; already surfaced via per-zone efficacy / calibration state / deficit / refill sensors |

- Record-carrying attributes are listed in `_unrecorded_attributes` (keeps large
  blobs out of the recorder DB).
- `coordinator.py`: the scheduler pushes records to it directly; all
  state-change watching and efficacy-file reads are removed. `parse_*` helpers
  remain, taking dicts.

## Cutover runbook (operator box)

**Before** (~10 min): snapshot `irrigation_efficacy.json` + `last_nightly` over
read-only SSH as a baseline; grep the box's live `automations.yaml` for
`pyscript.geodrops_rachio_*`; open a config-repo PR repointing
`pyscript.geodrops_rachio_last_nightly` → `sensor.geodrops_rachio_last_run` and
`pyscript.geodrops_rachio_status` → `sensor.geodrops_rachio_status` (merge right
after upgrade).

**Upgrade** (daytime, 10:00–20:00): HACS Download v1.0.0 → restart → pull the
dashboard PR.

**Day-one checks** (~15 min):
1. Log shows migration summary; `/config/pyscript/geodrops_rachio.py` is gone.
2. Per-zone Calibration State / efficacy match the snapshot.
3. Press Preview → `sensor.geodrops_rachio_plan` lists the zones yesterday's plan
   did, with minutes plausible for today's moisture.
4. Lawn Ops dashboard renders with no missing-entity errors.

**First night** (supervised): 23:00 plan published and status Waiting; morning
`last_run` shows delivered ≈ planned, no `aborted_reason`, Rachio call count in
the usual range; after 09:00 settle accepts/rejects observations normally.

**Rollback** (~5 min): HACS Redownload v0.9.15 → restart (delivery rewrites the
pyscript script, which reads the untouched state dir); revert the dashboard PR.
Cost: calibration learned since cutover is lost.

## Testing

| Layer | Covers | Runs |
|---|---|---|
| `tests_brain/` | Pure logic (279 tests). conftest imports `brain` as a standalone package so HA is not imported | Windows native + Docker |
| Engine tests (`FakePort`, simulated clock) | Normal night, rain abort, manual stop, standby, restart recovery from marker, window-start drop, sensor-recovery add, pause/resume collapse, Rachio-drop recovery. Written before each function is ported | Windows native + Docker |
| Replay parity | Real `last_nightly` records → native planning reproduces the recorded plan (zones + minutes) | Windows native + Docker |
| HA integration tests | Trigger wiring, unload cancels run + stops valves, buttons → scheduler, migration, pyscript retirement (reload only if service exists), Store survives reload, new sensors | Docker |

### Open risks (resolve in plan task 1)

1. **Replay input coverage.** Unverified whether `last_nightly` carries every
   planning input (sensor values, weather, efficacy snapshot). If not,
   reconstruct inputs from the recorder DB at the plan timestamp.
2. **Public repo.** Replay fixtures must be scrubbed: real zone names, entity
   ids and Rachio zone ids replaced with generic ones.

## Release

- Sequencing: `feature/options-flow-hub-refactor` merges first (both touch
  `__init__.py`); rebase this branch onto main afterwards.
- Single release **v1.0.0**, hand-written notes + CHANGELOG entry:
  breaking entity changes (`pyscript.*` → `sensor.*_last_run` / `sensor.*_plan`),
  pyscript no longer required, restart required, rollback instructions.
- README: remove pyscript prerequisite. Repo topics: drop `pyscript`.
- Min HA stays 2026.3.0.
- Estimate: ~12–15 plan tasks, 3–4 subagent-driven sessions (+~½ day replay parity).
