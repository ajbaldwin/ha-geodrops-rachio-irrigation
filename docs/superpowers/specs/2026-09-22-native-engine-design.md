# Native Engine (Remove pyscript) — Design

Date: 2026-09-22
Status: Approved (amended 2026-09-22 after full code read)
Target release: v1.0.0 (breaking)
Branch: `feature/native-engine` (the options-flow hub refactor already merged in v0.9.14 — no sequencing constraint)

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
| `engine/port.py` | `HAPort` protocol (`state`, `attrs`, `last_updated`, `call`, `sleep`, `now`) + `HassPort` (real). Tests supply `FakePort` over a simulated world | new |
| `engine/base.py` | `EngineBase`: the app's module globals as instance state, config loading, record/status publishing, listeners | new + ported (app 121–213) |
| `engine/io.py` | `IOMixin`: Rachio zone I/O, counters, runtime cache, efficacy / pending-obs access | ported (app 215–520) |
| `engine/runner.py` | `RunnerMixin`: `run_plan`, `run_collapsed`, `_walk_segment`, `_sleep_watching`, drain/probe helpers | ported (app 522–938) |
| `engine/planning.py` | `PlanningMixin`: abort predicates, weather/sensor/sun reads, drought profile, target floors, `_plan_context`, rain-skip check | ported (app 941–1532, 1716–1752) |
| `engine/orchestration.py` | `OrchestrationMixin`: records, waiting marker, `_plan_and_run`, preview, zone logbook, recap/calendar | ported (app 1535–1714, 1755–2367) |
| `engine/learning.py` | `LearningMixin`: 06:00 forecast calibration + 30-min settle-and-learn | ported (app 2372–2459, 2467–2601) |
| `engine/scheduler.py` | `Scheduler`: composes the mixins; triggers, run-task lifecycle, startup recovery, button actions, unload safety | ported (app 2461–2847) + new |
| `engine/store.py` | `EngineStore` over HA `Store` + legacy import + pyscript retirement | new |
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
- **Faithful port.** Each app function becomes a mixin method of the same name
  (`self.` prefix, `async`/`await`); logic, messages and record shapes are
  unchanged. The preview's save/restore of `_current_cfg`/`_current_bindings`
  is kept as-is (still correct under async interleaving).
- **Service calls are non-blocking** (`blocking=False`), matching pyscript's
  `service.call` default — except the unload safety stop (blocking, 10 s bound).

## Runtime

### Trigger mapping

| pyscript | Native |
|---|---|
| `cron(0 23 * * *)` nightly | `async_track_time_change(hour=23, minute=0, second=0)` |
| `cron(0 6 * * *)` calibrate | `async_track_time_change(hour=6, minute=0, second=0)` |
| `cron(*/30 * * * *)` settle | `async_track_time_change(minute=[0, 30], second=0)` |
| `@time_trigger("startup")` + `task.sleep(30)` | `async_at_started` → startup task that keeps the same 30 s sleep |
| `@state_trigger("button.geodrops_rachio_stop")` | `StopButton.async_press` → `scheduler.request_stop()` (raises the manual-stop flag, exactly like today) |
| 5 `@service` actions | removed; buttons call `Scheduler` methods |
| `task.executor` / `@pyscript_compile` | removed (async HTTP; Store for persistence) |

All unsubscribe callbacks go through `entry.async_on_unload`.

### Run lifecycle

- Exactly one `self.run_task`. `_start_run(fn, *args)` cancels any existing run
  task and awaits its cancellation, then starts the new one with
  `entry.async_create_background_task` (replaces `task.unique("geodrops_rachio_run")`).
- **Stop ≠ cancel** (unchanged semantics): Stop raises `_manual_stop`; the watch
  loop turns it into a `manual-stop` abort that still writes records + recap.
  **Reset** cancels the run task, stops the device and zones, clears markers.
- Unload (reload, options change, shutdown) cancels the run task — same
  semantics as `pyscript.reload` today: a waiting run re-arms at startup from the
  waiting marker; an interrupted watering run is handled by the existing
  startup recovery (`run_active` marker + open-valve check).
- **One behaviour change — unload safety stop.** Cancellation is a
  `BaseException`, so the runners' `except Exception` teardown never runs; only
  `finally: set_run_active(False)` does. Cancelled mid-pause, Rachio auto-resumes
  the paused schedule within 60 min with nobody watching, and startup cannot see
  it (marker already cleared). This hole exists in pyscript today. Fix: if valves
  were watering when unload began, call `stop_device` + `stop_all` (blocking,
  10 s bound) after cancelling.

## State and entities

### Storage

One `homeassistant.helpers.storage.Store` per entry, key
`geodrops_rachio.<entry_id>`, saved immediately (no delay) on every mutation:

- `efficacy`, `pending_obs` (calibration history — the valuable data)
- `records`: `last_nightly`, `calibration`, `targets`, `preview` (the app's
  `PERSISTED` set)
- `waiting_marker`

In memory only, as today: the `status`, `last_run` and `runtimes` records and
the 6-hour Rachio runtime cache. Store reads return deep copies so the ported
code keeps the app's read-fresh-from-file semantics.

### Legacy import + pyscript retirement (every setup, idempotent)

1. Delete delivered files if present: `/config/pyscript/geodrops_rachio.py`,
   `geodrops_rachio_config.yaml`, `.geodrops_rachio_version`,
   `modules/geodrops_rachio_lib/`.
2. If anything was deleted and `pyscript.reload` exists, call it so a legacy
   script already loaded this boot is torn down (its startup handler sleeps 30 s
   then may re-arm a waiting run — without this, both engines could water the
   same night).
3. Import legacy JSON from `/config/pyscript/geodrops_rachio_state/` into the
   Store **when the Store is empty OR step 1 deleted something** (pyscript was
   the live engine since, so its state is the newest — this also makes a
   rollback → re-upgrade round trip correct).
4. Leave `geodrops_rachio_state/` in place (rollback needs it).
5. Log a summary: zones imported, files removed, pyscript reloaded or not.

`manifest.json`: drop `after_dependencies: ["pyscript"]`.

### Entities

| Before | After |
|---|---|
| `pyscript.geodrops_rachio_status` | existing `sensor.geodrops_rachio_status`, fed directly; gains a `status` attribute carrying the raw lowercase token |
| `pyscript.geodrops_rachio_last_nightly` | new `sensor.geodrops_rachio_last_nightly` — state = the record's value (zones watered, as today); attributes = full record, **same attribute names** (minus `friendly_name`) |
| `pyscript.geodrops_rachio_last_run` | new `sensor.geodrops_rachio_last_run` — same pattern (any trigger, incl. run_now) |
| `pyscript.geodrops_rachio_preview` | new `sensor.geodrops_rachio_plan` — same pattern |
| `pyscript.geodrops_rachio_{calibration,targets,runtimes}` | internal only; already surfaced via per-zone efficacy / calibration state / deficit / refill sensors |

- Record sensors set `_unrecorded_attributes = frozenset({MATCH_ALL})` (keeps
  large blobs out of the recorder DB).
- Logbook entries the app attached to `pyscript.geodrops_rachio_status` /
  `_calibration` attach to `sensor.geodrops_rachio_status`.
- `config_flow.REQUIRED_COMPONENTS` drops `pyscript` (only `rachio` remains).
- `coordinator.py`: the scheduler pushes records to it directly; all
  state-change watching and efficacy-file reads are removed. `parse_*` helpers
  remain, taking dicts.

## Cutover runbook (operator box)

**Before** (~10 min): snapshot `irrigation_efficacy.json` + `last_nightly` over
read-only SSH as a baseline; grep the box's live `automations.yaml` for
`pyscript.geodrops_rachio_*`; open a config-repo PR repointing
`pyscript.geodrops_rachio_last_nightly` → `sensor.geodrops_rachio_last_nightly`
and `pyscript.geodrops_rachio_status` → `sensor.geodrops_rachio_status` — check
the 6 status refs for lowercase comparisons and read `state_attr(..., 'status')`
there (merge right after upgrade).

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
| `tests_brain/` | Pure logic (279 tests). conftest puts the integration dir on `sys.path` and imports `brain` standalone | Windows native + Docker |
| Engine unit tests | Each mixin against `FakeWorld`/`FakePort`: a simulated HA state machine, a simulated Rachio controller (schedule queue, pause/auto-resume, stop, drop and never-start injection) and a freezegun clock | Docker |
| **Differential tests vs the legacy app** | The old `geodrops_rachio.py` is kept verbatim as `tests/legacy/geodrops_rachio_legacy.py` and executed with fake pyscript globals (`state`, `service`, `task`, `log`, `logbook`, identity decorators) against an identical `FakeWorld`. Both engines run the same scenario; tests assert identical Rachio/notify/calendar/logbook calls, status transitions, published records and persisted state | Docker |
| HA integration tests | Trigger wiring, unload cancels run + safety stop, buttons → scheduler, legacy import + retirement, Store survives reload, new sensors, config flow without pyscript | Docker |

Differential scenarios: normal night, standby, rain skip at plan / at window
start, moisture-risen drop, sensor-recovery add, run_now, preview during wait,
manual stop, rain abort, external stop, never-started, Rachio drop + recovery,
non-collapse `run_plan` path, settle-and-learn (accept / training / reject /
expired), 06:00 calibration, startup (collapsed marker, orphan valve, waiting
re-arm, missed).

The differential layer replaces the earlier "replay parity" idea: it exercises
the valve loops as well as planning, needs no box data (fixtures are synthetic,
so nothing to scrub from a public repo), and removes both earlier open risks.
The legacy fixture is deleted in the first release after v1.0.0.

## Release

- Single release **v1.0.0**, hand-written notes + CHANGELOG entry:
  breaking entity changes (`pyscript.*` → `sensor.*_last_run` / `sensor.*_plan`),
  pyscript no longer required, restart required, rollback instructions.
- README: remove pyscript prerequisite. Repo topics: drop `pyscript`.
- Min HA stays 2026.3.0.
- Estimate: 13 plan tasks, 3–4 subagent-driven sessions.
