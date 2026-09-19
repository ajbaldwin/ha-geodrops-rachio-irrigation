# Sensor-recovery probe — design note

Status: **spec** (2026-09-18). A calibration-only feature: fold a probe for a
zone whose moisture sensor was unusable at plan time but **recovers** during the
pre-dawn wait, into that same night's run — so a flaky sensor doesn't cost a
night of calibration convergence.

Lives in the brain (`custom_components/geodrops_rachio/bundled_app/`), edited
directly; pure helpers unit-tested in `tests_brain/`.

## Background — one plan, then a long wait

`_plan_and_run(wait=True, ...)` (the nightly `cron(0 23 * * *)`) builds the plan
once at 23:00 via `_plan_context`, then `task.sleep`s until the pre-dawn window
and waters. Two facts drive this design:

1. **The run occupies the *tail* of the window.** `_plan_context` sets
   `start = end − span` (and `earliest_start = end − cap`), and the nightly
   sleeps to `start`, so watering runs `[start, end]` and finishes at the window
   close (`end` = sunrise/dawn − `end_offset`). The idle slack is the **front**,
   `[earliest_start, start]` — often hours (e.g. 4h20m idle, 40min run).
2. **A zone with a bad sensor at 23:00 is skipped for the whole night.** In
   `_plan_context`, `sensors.read_zone` returning `not online` (offline /
   low-quality) makes the zone `uncompleted[key] = offline_reason` and it never
   enters `priority` — so no plan, and (for a calibrating zone) no probe. Sitting
   just above the probe ceiling is a *different* skip; this feature is only about
   the **sensor** being unusable, then usable.

## Problem — a recovered sensor is a lost calibration night

GeoDrops dominant sensors drop out and report sparsely (hours between reports;
`unavailable` / low-quality states observed on the box, e.g. Left Wall skipped
`low_quality` on 2026-09-17). A calibrating zone whose sensor is bad at 23:00 but
reports a good value at, say, 02:40 could have been probed that night — the
window is still open — but nothing re-checks it. That night's convergence
opportunity is lost, and for a chronically flaky sensor this repeats.

Overnight soil barely *dries* (cool, dark, no ET, often dew), so the symmetric
"zone dried into range" case is rare and already covered by the 23:00 live read.
The recurring, worth-catching case is **sensor recovery**.

## Non-goals

- **Deficit watering.** Only calibration probes fold in. A zone that needs a real
  refill but had a bad sensor waits for the next nightly plan (≤ ~1 day; the
  agronomic cost of one night is low, and it avoids a second deficit path).
- **Recoveries during or after the main run.** The run holds the window tail; a
  probe added there would overrun the pre-dawn close. Those defer to next night.
- **Re-dosing / re-selecting already-planned zones.** The window-start moisture
  re-check ([[window-start-moisture-recheck]]) already handles the wet direction
  at run-start; this only *adds* recovered calibrating zones.

## Design — a re-planning watch across the idle front

Replace the single `task.sleep(wait_s)` before the run with a **bounded
re-plan loop** over `[now, start]`. On each wake it looks for a recovered,
probe-eligible calibrating zone and, if found, folds a probe in and **re-arms
`start` earlier** so the enlarged run still finishes by `end`.

### Loop (nightly, `wait=True`, self-calibration on, night not skipped)

```
# after the plan + the existing pre-wait rain skip check
recovery_inputs = capture once at plan time:
    - candidates: zones that are calibrating/recalibrating, not excluded, and
      were skipped at plan for a SENSOR reason (offline / low-quality) — i.e.
      uncompleted[k] in the offline reasons AND rec.state in {calibrating,recalibrating}
    - api_runtimes / api_spans / api_depths (already fetched in _plan_context;
      pass them out in ctx so the loop reuses them — no per-poll Rachio calls)
    - end, efficacy store snapshot, tun

while dt.now(tz) < start:
    task.sleep(min(RECOVERY_POLL_S, seconds_until(start)))
    if now >= start:            # reached the run; leave the loop
        break
    for zone_key in remaining candidates:
        reading = sensors.read_zone(zone_cfg, _read_zone_signals(zone_cfg))
        if not reading.online:                      # still bad — keep watching
            continue
        if not calibration.should_probe(state, reading.dominant, pinned, tun):
            drop candidate (converged path / above ceiling → nothing to gain)
            continue
        pm = probe_minutes(...) capped by cap_for_saturation, min full_refill
        if not fits_window(now, end, current_span + pm + soak_margin):
            continue            # not enough window left even now — defer, keep watching
        add zone_key to the plan's zone set with minutes[zone_key] = pm,
            dosing_sources[zone_key] = "probe", doses[zone_key] = probe DoseResult
        rebuild the_plan = plan.build_plan(zones, minutes, geo, adjacency, cap, tun)
        start = end − the_plan.span_minutes         # re-arm earlier
        record recovery_added[zone_key] = {"dominant": reading.dominant}
        drop zone_key from candidates (one probe per zone per night)
# fall through to the existing window-start rain re-check + moisture drop
# re-check + run, now with the (possibly) enlarged plan and earlier start
```

`start` only moves **earlier** (adding zones grows `span`), so the loop always
converges toward the run; `fits_window` guarantees the enlarged run still ends by
`end`. When no sensor ever recovers, the loop is a chunked no-op sleep — behaviour
identical to today.

**Empty-plan night.** If 23:00 planned nothing (every online zone above floor, or
the only calibrating zone had a bad sensor), the base plan is empty: `span = 0`,
`start = end`, and today the nightly already sleeps to `end` and runs nothing. The
loop makes that idle wait productive — it watches the whole `[now, end]` span, and
a sensor that recovers turns an otherwise-wasted night into a probe. `build_plan`
still caps `span` at `cap`, so a re-armed `start` can never precede
`earliest_start` (the run stays inside the pre-dawn window).

### Why a re-plan loop, not re-calling `_plan_context`

`_plan_context` has side effects — it stamps `excluded_since`, runs
`exclusion_return`, and writes the efficacy store. Re-running it every poll would
repeatedly touch calibration state during the wait. The loop instead uses the
**pure** helpers (`should_probe`, `probe_minutes`, `cap_for_saturation`,
`plan.build_plan`) against an in-memory snapshot and writes nothing to the store
until the run appends its pending obs as usual.

### Folding the probe in

An added zone rides the **single collapsed run** (`run_collapsed`) like any other
probe zone — one Rachio schedule, one notification. After the run, the existing
`self_calibration_enabled and watered` block appends a pending obs for it
(`pre_dominant` = the recovery reading, `minutes` = probe minutes), so
`_settle_and_learn` learns from it on the normal schedule. No new watering path.

## Constants / tunables

- `RECOVERY_POLL_S` — watch cadence during the idle front. Start at **1800 s**
  (30 min), matching the sensor cadence and the existing settle poll; a new
  `Tunables` field `recovery_poll_seconds` (default 1800) keeps it adjustable.
- `soak_margin` — reuse the plan's own cycle/soak sizing via `build_plan`, so no
  separate constant; `fits_window` checks the rebuilt `span_minutes`.
- Gate the whole feature on `self_calibration_enabled` (already the probe gate).

## Restart safety

The wait already persists a marker (`_write_waiting_marker(end_iso, stamp,
trigger)`) so a restart mid-wait re-arms via `_on_startup`. The loop keeps that
marker fresh (its `end` is unchanged; only `start` moves, and start is derived,
not persisted). A restart during the loop re-enters the nightly path, which
rebuilds the plan from live moisture — a recovered zone is simply included from
the top. No new persisted state is required.

## Telemetry

`last_nightly` gains `recovery_added` (`{zone: {dominant}}`, absent when none),
mirroring `window_start_dropped`, so a folded-in probe is visible after the fact.

## Testing

Pure, in `tests_brain/`:
- `recovery_candidate(state, excluded, uncompleted_reason)` (new pure predicate):
  true only for calibrating/recalibrating, non-excluded, sensor-skipped zones.
- `fits_window(now, end, span_minutes)`: boundary at exact fit; false when the
  enlarged run would end after `end`.
- probe sizing reuses already-tested `probe_minutes` / `cap_for_saturation`.
- `plan.build_plan` rebuild with an added zone is covered by existing plan tests.

App wiring (`_plan_and_run` loop) stays `py_compile`-only, per repo practice for
pyscript-primitive code.

## Out of scope / future

- Standalone late probes (recovery during/after the main run) — deliberately
  excluded; would need a second watering path and a window-close compromise.
- Deficit (non-probe) folding — excluded; ≤1-day nightly latency is acceptable.
