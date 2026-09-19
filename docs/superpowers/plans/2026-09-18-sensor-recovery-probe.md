# Sensor-Recovery Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold a calibration probe for a zone whose moisture sensor was unusable at 23:00 but recovers during the pre-dawn wait into that same night's run.

**Architecture:** Replace the nightly pre-run `task.sleep` with a bounded re-plan loop across the idle front. Each poll re-reads the sensors of calibrating zones that were sensor-skipped at plan time; when one recovers and is probe-eligible and still fits the window, it folds a probe into the plan (via `plan.build_plan`) and re-arms `start` earlier so the enlarged run still ends by window close. Decisions use pure, unit-tested helpers; the loop wiring is pyscript app code (py_compile-only).

**Tech Stack:** Python 3.13, pyscript (Home Assistant). Pure logic in `bundled_app/geodrops_rachio_lib/`, app in `bundled_app/geodrops_rachio.py`, pure tests in `tests_brain/` (run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain`), full suite in Docker via `bash tools/test.sh`.

**Spec:** `docs/design/sensor-recovery-probe.md`

## Global Constraints

- Brain source is edited directly in `custom_components/geodrops_rachio/bundled_app/`; the lib package is `geodrops_rachio_lib` (namespaced). No `irrigation_lib` imports.
- Pure logic must run without the HA stack (no pyscript globals `state`/`service`/`task`/`log`, no generator expressions — pyscript interprets the lib; use list comprehensions).
- App-file (`geodrops_rachio.py`) changes cannot be unit-tested (pyscript primitives); verify with `python -m py_compile` and the full Docker suite.
- Feature is gated on `tunables.self_calibration_enabled` (already the probe gate); default-off installs are unaffected.
- Sensor-skip reasons are exactly `{"unavailable", "low_quality"}` (from `sensors.read_zone`).
- Probe dosing reuses existing pure helpers: `calibration.probe_minutes`, `calibration.cap_for_saturation`, `plan.cycles_minutes`; a probe's `DoseResult.source` is `"probe"`.

---

### Task 1: `recovery_candidate` pure predicate

**Files:**
- Modify: `custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib/evaluate.py`
- Test: `tests_brain/test_evaluate.py`

**Interfaces:**
- Produces: `evaluate.recovery_candidate(state: str, uncompleted_reason, sensor_skip_reasons) -> bool` — True iff the zone is calibrating/recalibrating AND was skipped for a sensor reason (so it's worth re-checking as its sensor may recover).

- [ ] **Step 1: Write the failing tests**

Append to `tests_brain/test_evaluate.py`:

```python
# --- sensor-recovery candidate predicate ---

_SKIP = frozenset({"unavailable", "low_quality"})


def test_recovery_candidate_calibrating_sensor_skipped():
    assert evaluate.recovery_candidate("calibrating", "unavailable", _SKIP) is True
    assert evaluate.recovery_candidate("recalibrating", "low_quality", _SKIP) is True


def test_recovery_candidate_rejects_non_calibrating_state():
    assert evaluate.recovery_candidate("converged", "unavailable", _SKIP) is False


def test_recovery_candidate_rejects_non_sensor_reason():
    # Skipped for exclusion or above-floor, not a bad sensor: not a candidate.
    assert evaluate.recovery_candidate("calibrating", "excluded", _SKIP) is False
    assert evaluate.recovery_candidate("calibrating", None, _SKIP) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_evaluate.py -k recovery_candidate -v`
Expected: FAIL with `AttributeError: module 'geodrops_rachio_lib.evaluate' has no attribute 'recovery_candidate'`

- [ ] **Step 3: Write minimal implementation**

Add to `evaluate.py` (after `revalidate_zone`):

```python
def recovery_candidate(state, uncompleted_reason, sensor_skip_reasons):
    """True iff a zone is worth re-checking during the pre-dawn wait: it is
    calibrating/recalibrating AND was skipped from tonight's plan for a SENSOR
    reason (offline / low-quality), so a mid-window sensor recovery could still
    earn a probe. Excluded ("excluded") and above-floor (None) skips are not
    candidates.
    """
    if state not in ("calibrating", "recalibrating"):
        return False
    return uncompleted_reason in sensor_skip_reasons
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_evaluate.py -v`
Expected: PASS (all, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib/evaluate.py tests_brain/test_evaluate.py
git commit -m "feat: recovery_candidate predicate for sensor-recovery probe"
```

---

### Task 2: `fits_window` pure helper

**Files:**
- Modify: `custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib/plan.py`
- Test: `tests_brain/test_plan.py`

**Interfaces:**
- Produces: `plan.fits_window(now, end, span_minutes) -> bool` — True iff a run of `span_minutes` starting at `now` finishes at or before `end`. `now` and `end` are tz-aware datetimes.

- [ ] **Step 1: Write the failing tests**

Append to `tests_brain/test_plan.py`:

```python
import datetime as _dt


def _t(h, m):
    return _dt.datetime(2026, 9, 18, h, m, tzinfo=_dt.timezone.utc)


def test_fits_window_true_when_run_ends_before_end():
    assert plan.fits_window(_t(3, 0), _t(6, 0), 90) is True


def test_fits_window_boundary_exact_fit_true():
    assert plan.fits_window(_t(4, 30), _t(6, 0), 90) is True


def test_fits_window_false_when_run_overruns_end():
    assert plan.fits_window(_t(5, 0), _t(6, 0), 90) is False
```

(If `test_plan.py` already imports `plan`, reuse that import; the helpers above only add `datetime`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_plan.py -k fits_window -v`
Expected: FAIL with `AttributeError: module 'geodrops_rachio_lib.plan' has no attribute 'fits_window'`

- [ ] **Step 3: Write minimal implementation**

Add to `plan.py` (top-level, after `cycles_minutes`):

```python
def fits_window(now, end, span_minutes) -> bool:
    """True iff a run of `span_minutes` beginning at `now` finishes at or before
    `end` (the window close). Boundary inclusive. Used to decide whether a
    recovered zone's probe still fits before the pre-dawn window shuts.
    """
    import datetime as _dt
    return now + _dt.timedelta(minutes=span_minutes) <= end
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_plan.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib/plan.py tests_brain/test_plan.py
git commit -m "feat: fits_window helper for window-fit checks"
```

---

### Task 3: `recovery_poll_seconds` tunable

**Files:**
- Modify: `custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib/config.py`
- Test: `tests_brain/test_config.py`

**Interfaces:**
- Produces: `Tunables.recovery_poll_seconds: float` (default `1800.0`).

- [ ] **Step 1: Write the failing tests**

Append to `tests_brain/test_config.py`:

```python
def test_recovery_poll_seconds_default():
    assert config.Tunables().recovery_poll_seconds == 1800.0


def test_recovery_poll_seconds_override():
    assert config.Tunables(recovery_poll_seconds=600).recovery_poll_seconds == 600
```

(If `test_config.py` imports `config` differently, match its existing import.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_config.py -k recovery_poll -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'recovery_poll_seconds'`

- [ ] **Step 3: Write minimal implementation**

In `config.py`, in the `Tunables` dataclass, add after `recalibrate_after_exclusion_hours`:

```python
    # How often (seconds) the pre-dawn wait re-checks calibrating zones that were
    # skipped for a bad sensor, to fold in a probe if the sensor recovers before
    # the window closes. Matches the GeoDrops report cadence and the settle poll.
    recovery_poll_seconds: float = 1800.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib/config.py tests_brain/test_config.py
git commit -m "feat: recovery_poll_seconds tunable (default 1800)"
```

---

### Task 4: Expose recovery inputs in `_plan_context` ctx

**Files:**
- Modify: `custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py`

**Interfaces:**
- Consumes: `evaluate.recovery_candidate` (Task 1).
- Produces: module constant `SENSOR_SKIP_REASONS`; ctx keys `api_runtimes`, `api_depths`, `recovery_candidates` (list of zone keys).

- [ ] **Step 1: Add the module constant**

Near the top of `geodrops_rachio.py`, beside the other module-level constants, add:

```python
# Offline reasons that mean the SENSOR was unusable at plan time (as opposed to
# "excluded" or being above target). A calibrating zone skipped for one of these
# is a sensor-recovery probe candidate. Mirrors sensors.read_zone.
SENSOR_SKIP_REASONS = frozenset(("unavailable", "low_quality"))
```

- [ ] **Step 2: Compute the candidate list in `_plan_context`**

In `_plan_context`, immediately before the `return { ... }` dict, add:

```python
    # Calibrating zones skipped tonight for a bad sensor — the pre-dawn wait
    # re-checks these and folds in a probe if the sensor recovers.
    recovery_candidates = []
    if tun.self_calibration_enabled:
        for _k in cfg.zones:
            _rec = efficacy_store.get(_k) or {}
            if evaluate.recovery_candidate(
                    _rec.get("state", "calibrating"),
                    uncompleted.get(_k), SENSOR_SKIP_REASONS):
                recovery_candidates.append(_k)
```

- [ ] **Step 3: Add the three keys to the ctx return**

In the `return { ... }` of `_plan_context`, add these entries alongside the existing `"floors"` key:

```python
        "api_runtimes": api_runtimes,
        "api_depths": api_depths_for_dose,
        "recovery_candidates": recovery_candidates,
```

- [ ] **Step 4: Verify it compiles**

Run: `python -m py_compile custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py`
Expected: no output (exit 0)

- [ ] **Step 5: Verify the brain suite still passes**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain -q`
Expected: PASS (unchanged count; this task adds no brain tests)

- [ ] **Step 6: Commit**

```bash
git add custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py
git commit -m "feat: expose recovery-probe inputs in plan ctx"
```

---

### Task 5: Re-plan watch loop + telemetry in `_plan_and_run`

**Files:**
- Modify: `custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py`

**Interfaces:**
- Consumes: `evaluate.recovery_candidate` (Task 1, via ctx list), `plan.fits_window` (Task 2), `tun.recovery_poll_seconds` (Task 3), ctx keys `api_runtimes`/`api_depths`/`recovery_candidates` (Task 4), and existing pure helpers `calibration.should_probe`, `calibration.probe_minutes`, `calibration.cap_for_saturation`, `plan.cycles_minutes`, `plan.build_plan`, `dosing.DoseResult`.
- Produces: ctx key `recovery_added` (`{zone: {"dominant": float}}`); `last_nightly.recovery_added` telemetry.

- [ ] **Step 1: Replace the single pre-run sleep with the re-plan loop**

In `_plan_and_run`, inside `if wait:`, the current block writes the waiting marker then calls `task.sleep(wait_s)`. Replace **only** the single `task.sleep(wait_s)` call with the loop below (keep the surrounding marker write/clear and the `_current_cfg`/`_current_bindings` re-establish that follow):

```python
                # Re-plan watch across the idle front: fold in a probe for any
                # calibrating zone whose bad sensor recovers before the window
                # closes. Adding a zone only ever moves `start` EARLIER (span
                # grows), so the loop still converges to the run; fits_window
                # keeps the enlarged run inside the window. No recovery -> this
                # is a chunked no-op sleep, identical to a plain wait.
                poll_s = tun.recovery_poll_seconds
                watching = tun.self_calibration_enabled and bool(
                    ctx["recovery_candidates"])
                pending_recovery = list(ctx["recovery_candidates"]) if watching else []
                now_w = dt.datetime.now(start.tzinfo)
                while now_w < start:
                    nap = (start - now_w).total_seconds()
                    if pending_recovery:
                        nap = min(nap, poll_s)
                    if nap > 0:
                        task.sleep(nap)
                    now_w = dt.datetime.now(start.tzinfo)
                    if now_w >= start or not pending_recovery:
                        continue
                    rstore = _read_efficacy_store()
                    still_pending = []
                    for rk in pending_recovery:
                        rzc = cfg.zones[rk]
                        rreading = sensors.read_zone(rzc, _read_zone_signals(rzc))
                        if not rreading.online:
                            still_pending.append(rk)
                            continue
                        rrec = rstore.get(rk) or {}
                        rpinned = rzc.refill_span_pts > 0
                        if not calibration.should_probe(
                                rrec.get("state", "calibrating"),
                                rreading.dominant, rpinned, tun):
                            continue  # converged or above ceiling: nothing to gain
                        rbase = ctx["api_runtimes"].get(rzc.rachio_zone_id) or rzc.runtime_minutes
                        rfull = plan.cycles_minutes(rbase, 1.0)
                        rpm = calibration.probe_minutes(
                            rfull, rrec.get("prior_minutes"), rrec.get("last_rise"), tun)
                        rpm = calibration.cap_for_saturation(
                            rpm, rreading.dominant, rrec.get("efficacy"), tun)
                        rpm = min(rpm, rfull)
                        trial_minutes = dict(ctx["minutes"])
                        trial_minutes[rk] = rpm
                        trial_zones = list(priority) + [rk]
                        rgeo = {z: cfg.zones[z].geography for z in trial_zones}
                        radj = {z: cfg.zones[z].adjacency for z in trial_zones}
                        trial_plan = plan.build_plan(
                            trial_zones, trial_minutes, rgeo, radj,
                            ctx["cap_minutes"], tun)
                        if not plan.fits_window(now_w, ctx["end"], trial_plan.span_minutes):
                            still_pending.append(rk)  # no room now; keep watching
                            continue
                        # Commit the probe into the live plan.
                        rdepth = ctx["api_depths"].get(rzc.rachio_zone_id) or rzc.refill_depth_mm
                        rfrac = min(rpm / rfull, 1.0) if rfull > 0 else 0.0
                        ctx["minutes"][rk] = rpm
                        ctx["doses"][rk] = dosing.DoseResult(
                            minutes=rpm, frac=rfrac,
                            effective_depth_mm=rfrac * float(rdepth),
                            deficit_pts=0.0, span_pts=0.0, source="probe")
                        ctx["dosing_sources"][rk] = "probe"
                        # The zone was skipped at plan time, so it has no entry in
                        # dominant_by_zone. The post-run pending-obs block reads
                        # pre_dominant from there; without this the settle poll
                        # sees pre_dominant=None and drops the obs (probe waters
                        # but never calibrates). Record the recovery reading as the
                        # pre-watering moisture.
                        ctx["dominant_by_zone"][rk] = rreading.dominant
                        priority.append(rk)
                        the_plan = trial_plan
                        ctx["the_plan"] = the_plan
                        start = ctx["end"] - dt.timedelta(minutes=the_plan.span_minutes)
                        ctx["start"] = start
                        ctx.setdefault("recovery_added", {})[rk] = {
                            "dominant": rreading.dominant}
                        _activity(
                            f"Recovery probe folded in: {rk} "
                            f"(dominant {rreading.dominant}, {int(rpm)} min)")
                        _set_status(
                            "waiting",
                            detail=f"watering starts {start.astimezone():%H:%M}")
                    pending_recovery = still_pending
                    now_w = dt.datetime.now(start.tzinfo)
```

- [ ] **Step 2: Keep `ctx["the_plan"]` in sync in the existing drop re-check**

In the window-start moisture drop block (added by the earlier fix), after `the_plan = plan.build_plan(survivors, surv_minutes, geo, adjacency, ctx["cap_minutes"], tun)`, add:

```python
                    ctx["the_plan"] = the_plan
```

Rationale: `_publish_last_run` reads `ctx["the_plan"].watered` for `planned_zones`/`dosing`/`calibration`. Both the drop re-check and the recovery loop mutate the local `the_plan`; syncing `ctx["the_plan"]` keeps the record's planned set accurate (otherwise a dropped zone still shows as planned, and a folded-in probe does not show at all).

- [ ] **Step 3: Emit the recovery telemetry**

In `_publish_last_run`, inside the `if ctx is not None:` block, right after the `window_start_dropped` emit, add:

```python
        if ctx.get("recovery_added"):
            attributes["recovery_added"] = ctx["recovery_added"]
```

- [ ] **Step 4: Verify it compiles**

Run: `python -m py_compile custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py`
Expected: no output (exit 0)

- [ ] **Step 5: Run the full suite in Docker**

Run: `bash tools/test.sh`
Expected: PASS (360 pre-existing + the new pure tests from Tasks 1–3; e.g. `366 passed`)

- [ ] **Step 6: Commit**

```bash
git add custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py
git commit -m "feat: fold recovered-sensor calibration probes into the nightly run"
```

---

## Notes for the executor

- **Do not release in this plan.** No manifest bump, no tag — the maintainer batches releases. Leave `manifest.json` untouched.
- **Order matters:** Tasks 1–3 (pure, tested) must land before Tasks 4–5 (app wiring that imports them), because the app cannot be unit-tested and relies on those helpers being correct.
- **pyscript reminders while editing the app file:** no generator expressions (use list comprehensions); nested defs/lambdas cannot see enclosing locals; keep new logic inline in `_plan_and_run` (do not extract to a sibling module — only `geodrops_rachio.py` gets `state`/`service`/`task`/`log`).
- **Loop invariant to preserve:** every path that mutates `the_plan` must also set `ctx["the_plan"]` (and `ctx["start"]` when `start` moves) so telemetry stays truthful.
