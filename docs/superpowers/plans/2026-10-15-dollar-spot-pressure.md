# Dollar Spot Disease Pressure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Smith-Kerns dollar spot flag to the disease-pressure count. It is computed from recorder daily means of the bound temperature and humidity sensors, and it shrinks the watering window in spring and fall the way warm nights already do in summer.

**Architecture:**

- **Pure model:** `brain/dollar_spot.py` computes the 5-day means and the probability.
- **Cap:** `brain/weather.py` folds the flag into the existing temperature signal with "or". The count stays 0–3.
- **Engine cache:** the engine keeps the latest `DiseaseRisk` in `self.disease_risk`. A new `DiseaseRiskMixin` refreshes it hourly through an injected `fetch_daily_means` callable.
- **HA side:** `disease_stats.py` implements that callable with recorder long-term statistics.
- **Fail-open rule:** with no risk available, every output is byte-identical to today.

**Tech Stack:** Python 3.13, Home Assistant custom integration (min HA 2026.3.0), recorder `statistics_during_period`, pytest + pytest-homeassistant-custom-component + freezegun, Docker test image `geodrops-rachio-test`.

**Spec:** `docs/superpowers/specs/2026-10-15-dollar-spot-pressure-design.md`

## Global Constraints

- **Repo and branch:** worktree of `ajbaldwin/ha-geodrops-rachio-irrigation`, branch `claude/dynamic-seasonal-watering-edc37c`. Commit per task. Don't push until Task 8.
- **Commit identity:** `git config user.email` must be `15436467+ajbaldwin@users.noreply.github.com`. Check it before the first commit. Messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Abbreviation:** `CC` means `custom_components/geodrops_rachio`.
- **Fail-open invariant:** when `disease_risk` is `None` or `dollar_spot_enabled` is false, pressure breakdowns, caps, records and recaps must be identical to today.
  - `PressureBreakdown` gains its new keys only when a probability is passed.
  - The golden fixtures may change only by the two new `Tunables` fields and the new `dollar_spot_pct` ctx key.
  - Any other golden diff is a bug.
- **No CHANGELOG edit.** The release PR writes it.
- **Test commands:**
  - Brain (Windows native OK): `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain -q`
  - Everything else (Docker Desktop running): `bash tools/test.sh <args>`
  - Golden regeneration: `GOLDEN_UPDATE=1 bash tools/test.sh tests/engine -q`, then `git diff tests/engine/golden`

## File Map

| File | Change |
|---|---|
| `CC/brain/dollar_spot.py` | **New.** Model constants, `DiseaseRisk`, `probability_pct`, `five_day_means`, `assess`. |
| `CC/brain/config.py` | Two tunables. |
| `CC/brain/weather.py` | `dollar_spot_pct` parameter on `pressure_breakdown` / `pressure_signals` / `window_cap_minutes`. |
| `CC/engine/base.py` | `FetchDailyMeans` type, optional ctor arg, `self.disease_risk`. |
| `CC/engine/disease_risk.py` | **New.** `DiseaseRiskMixin.async_refresh_disease_risk`. |
| `CC/engine/planning.py` | `_dollar_spot_pct`; pass it at the cap, `_pressure_pair`, `project_window`; ctx key. |
| `CC/engine/orchestration.py` | Recap label in `_plan_summary`. |
| `CC/engine/scheduler.py` | Mixin, ctor arg, refresh at startup and on the hourly tick. |
| `CC/disease_stats.py` | **New.** Recorder daily means → `[(date, t_c, rh)]`. |
| `CC/__init__.py` | Inject the fetcher. |
| `CC/manifest.json` | `recorder` in `after_dependencies`. |
| `CC/sensor.py` | Watering window attributes. |
| `README.md` | Two tunable rows; one sentence on dollar spot. |
| Tests | `tests_brain/test_dollar_spot.py` (new), `tests_brain/test_weather.py`, `tests/engine/test_disease_risk.py` (new), `tests/engine/test_planning_reads.py`, `tests/test_disease_stats.py` (new), `tests/test_entities.py`, goldens. |

---

### Task 0: Commit the specs

**Files:** `docs/superpowers/specs/2026-10-15-dollar-spot-pressure-design.md`, `docs/superpowers/specs/2026-10-15-auto-watering-time-design.md`, this plan.

- [ ] Set the dollar spot spec's `Status:` to `Approved for planning`.
- [ ] Commit: `docs: dollar spot pressure spec + plan; park auto watering time`.

---

### Task 1: Pure model — `brain/dollar_spot.py`

**Files:** Create `CC/brain/dollar_spot.py` and `tests_brain/test_dollar_spot.py`.

- [ ] **Step 1: Write the failing tests** in `tests_brain/test_dollar_spot.py`:

```python
import datetime as dt

import pytest

from brain import dollar_spot as ds

TODAY = dt.date(2026, 10, 15)


def days(n, t=15.0, rh=80.0, start=1):
    return [(TODAY - dt.timedelta(days=i), t, rh) for i in range(start, start + n)]


def test_probability_known_values():
    assert ds.probability_pct(20.0, 80.0) == pytest.approx(40.4, abs=0.05)
    assert ds.probability_pct(15.0, 75.0) == pytest.approx(14.2, abs=0.05)


def test_probability_valid_only_between_10_and_35_c():
    assert ds.probability_pct(9.9, 90.0) is None
    assert ds.probability_pct(35.1, 90.0) is None
    assert ds.probability_pct(10.0, 90.0) is not None
    assert ds.probability_pct(35.0, 90.0) is not None


def test_five_day_means_uses_last_five_complete_days():
    daily = days(5, t=20.0, rh=80.0) + [(TODAY, 99.0, 99.0)]  # today excluded
    assert ds.five_day_means(daily, TODAY) == (20.0, 80.0, 5)


def test_five_day_means_tolerates_one_gap():
    daily = days(5)
    daily[2] = (daily[2][0], None, 80.0)
    assert ds.five_day_means(daily, TODAY) == (15.0, 80.0, 4)


def test_five_day_means_needs_four_days():
    assert ds.five_day_means(days(3), TODAY) is None


def test_five_day_means_ignores_days_older_than_five():
    daily = days(5, t=20.0) + days(3, t=0.0, start=6)
    assert ds.five_day_means(daily, TODAY)[0] == 20.0


def test_assess_builds_a_risk():
    risk = ds.assess(days(5, t=20.0, rh=80.0), TODAY)
    assert risk is not None
    assert risk.pct == pytest.approx(40.4, abs=0.05)
    assert (risk.t5_c, risk.rh5, risk.days) == (20.0, 80.0, 5)
    assert risk.as_of == "2026-10-14"


def test_assess_out_of_range_is_a_risk_with_no_probability():
    risk = ds.assess(days(5, t=5.0), TODAY)
    assert risk is not None and risk.pct is None


def test_assess_without_enough_days_is_none():
    assert ds.assess(days(2), TODAY) is None
```

- [ ] **Step 2:** Run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain/test_dollar_spot.py -q`. Expected: it fails with `ImportError` (no module `dollar_spot`).

- [ ] **Step 3: Implement** `CC/brain/dollar_spot.py`:

```python
"""Smith-Kerns dollar spot probability. Pure Python.

The model (UW-Madison Turfgrass Diagnostic Lab; the one behind Purdue's
Turfgrass Disease Prediction Tool) is a logistic regression on the 5-day moving
averages of daily mean relative humidity and air temperature:

    logit = -11.4041 + 0.0894 * RH5 + 0.1932 * T5(C)
    P     = e^logit / (1 + e^logit)

It is only valid for 10 C <= T5 <= 35 C; outside that the fungus is inactive.
Validated on creeping bentgrass golf turf, so the threshold applied to it is a
tunable starting point for lawns, not a calibrated value.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass

INTERCEPT = -11.4041
RH_COEF = 0.0894
TEMP_COEF = 0.1932
MIN_T5_C = 10.0
MAX_T5_C = 35.0
WINDOW_DAYS = 5
# A station offline for a day must not switch the signal off for five.
MIN_DAYS = 4

DailyMeans = Sequence[tuple[dt.date, float | None, float | None]]


@dataclass(frozen=True)
class DiseaseRisk:
    """The dollar spot assessment for the 5 complete days before `as_of`+1.

    `pct` is None when T5 is outside the model's valid range (not active)."""
    pct: float | None
    t5_c: float
    rh5: float
    days: int
    as_of: str  # ISO date of the most recent day used


def probability_pct(t5_c: float, rh5: float) -> float | None:
    """Smith-Kerns probability in percent; None outside 10-35 C."""
    if not MIN_T5_C <= t5_c <= MAX_T5_C:
        return None
    logit = INTERCEPT + RH_COEF * rh5 + TEMP_COEF * t5_c
    return 100.0 / (1.0 + math.exp(-logit))


def five_day_means(daily: DailyMeans,
                   today: dt.date) -> tuple[float, float, int] | None:
    """(T5 C, RH5 %, days used) over the WINDOW_DAYS complete local days before
    `today`. Complete days only, so the 23:00 plan and a 02:00 restart agree.
    None when fewer than MIN_DAYS of them have both values."""
    window = {today - dt.timedelta(days=i) for i in range(1, WINDOW_DAYS + 1)}
    by_day: dict[dt.date, tuple[float, float]] = {}
    for day, t_c, rh in daily:
        if day in window and t_c is not None and rh is not None:
            by_day[day] = (t_c, rh)
    if len(by_day) < MIN_DAYS:
        return None
    n = len(by_day)
    return (sum(v[0] for v in by_day.values()) / n,
            sum(v[1] for v in by_day.values()) / n, n)


def assess(daily: DailyMeans, today: dt.date) -> DiseaseRisk | None:
    """The risk for `today`, or None when there are not enough days."""
    means = five_day_means(daily, today)
    if means is None:
        return None
    t5_c, rh5, n = means
    return DiseaseRisk(pct=probability_pct(t5_c, rh5), t5_c=t5_c, rh5=rh5,
                       days=n, as_of=(today - dt.timedelta(days=1)).isoformat())
```

- [ ] **Step 4:** Rerun the test file. Expected: all pass.
- [ ] **Step 5:** Commit: `Add Smith-Kerns dollar spot model (brain)`.

---

### Task 2: Tunables

**Files:** Modify `CC/brain/config.py`, `README.md`, goldens.

- [ ] **Step 1:** In `Tunables`, directly after `stagnant_wind_mph`, add:

```python
    # Smith-Kerns dollar spot (brain/dollar_spot.py) counts toward the
    # temperature pressure signal at or above this probability. 20 % is the
    # UW-Madison / Purdue action threshold (golf turf); tune for lawns.
    dollar_spot_enabled: bool = True
    dollar_spot_threshold_pct: float = 20.0
```

- [ ] **Step 2:** In `README.md`'s advanced-overrides table, add after the `stagnant_wind_mph` row:

```
| `dollar_spot_enabled` | true | Count dollar spot risk (Smith-Kerns, from the last 5 days of temperature and humidity) as disease pressure. |
| `dollar_spot_threshold_pct` | 20.0 | Dollar spot probability that counts as pressure. |
```

- [ ] **Step 3:** In `tests_brain/test_config.py`, next to the existing `humid_rh_pct == 90` assertion, add `assert cfg.tunables.dollar_spot_enabled is True` and `assert cfg.tunables.dollar_spot_threshold_pct == 20.0`.
- [ ] **Step 4:** Run `GOLDEN_UPDATE=1 bash tools/test.sh tests/engine -q`, then `git diff --stat tests/engine/golden` and `git diff tests/engine/golden | grep '^[+-] ' | sort -u`. Expected: only the added `"dollar_spot_enabled": true` and `"dollar_spot_threshold_pct": 20.0` lines.
- [ ] **Step 5:** Run the brain tests and `bash tools/test.sh -q`. Expected: all pass.
- [ ] **Step 6:** Commit: `Add dollar spot tunables`.

---

### Task 3: Fold dollar spot into the pressure count — `brain/weather.py`

**Files:** Modify `CC/brain/weather.py` and `tests_brain/test_weather.py`.

- [ ] **Step 1: Write the failing tests.** Append to `tests_brain/test_weather.py`:

```python
def test_dollar_spot_counts_when_not_warm():
    w = wx(temp=55, rh=95, wind=10)  # fall night: humid only
    b = weather.pressure_breakdown(w, T, dollar_spot_pct=34.0)
    assert b["dollar_spot"] is True and b["dollar_spot_pct"] == 34.0
    assert b["count"] == 2
    assert weather.window_cap_minutes(w, T, dollar_spot_pct=34.0) == 4 * 60


def test_dollar_spot_adds_nothing_when_already_warm():
    w = wx(temp=75, rh=95, wind=1)
    assert weather.pressure_breakdown(w, T, dollar_spot_pct=80.0)["count"] == 3


def test_dollar_spot_below_threshold_does_not_count():
    b = weather.pressure_breakdown(wx(temp=55, rh=95), T, dollar_spot_pct=19.9)
    assert b["dollar_spot"] is False and b["count"] == 1


def test_no_dollar_spot_value_keeps_the_old_breakdown_exactly():
    w = wx(temp=55, rh=95)
    assert weather.pressure_breakdown(w, T, None) == weather.pressure_breakdown(w, T)
    assert "dollar_spot" not in weather.pressure_breakdown(w, T)
    assert weather.window_cap_minutes(w, T, None) == weather.window_cap_minutes(w, T)
```

- [ ] **Step 2:** Run the brain tests. Expected: the new tests fail (`unexpected keyword argument 'dollar_spot_pct'`).

- [ ] **Step 3: Implement.** In `CC/brain/weather.py`:

```python
from typing import Any, NotRequired, TypedDict
```

```python
class PressureBreakdown(TypedDict):
    warm: bool
    humid: bool
    stagnant: bool
    count: int
    # Present only when a dollar spot probability was supplied, so a breakdown
    # made without one is exactly what it was before dollar spot existed.
    dollar_spot: NotRequired[bool]
    dollar_spot_pct: NotRequired[float]


def pressure_breakdown(w: WeatherReading, t: Tunables,
                       dollar_spot_pct: float | None = None) -> PressureBreakdown:
    """Which disease-pressure signals are active, for diagnostics/reporting.

    Returns {"warm", "humid", "stagnant": bool, "count": int}. With a dollar
    spot probability it also carries "dollar_spot" and "dollar_spot_pct", and
    the temperature signal counts when EITHER the night is warm (brown patch,
    Purdue's night >= 68 F rule) OR dollar spot is at/above its threshold (the
    spring and fall diseases the 68 F rule misses). Still 0..3, so summer caps
    are unchanged: dollar spot adds nothing on a night that is already warm.
    """
    warm = w.temp_f > t.warm_temp_f
    humid = w.rh_pct > t.humid_rh_pct
    stagnant = w.wind_mph < t.stagnant_wind_mph
    out: PressureBreakdown = {
        "warm": warm, "humid": humid, "stagnant": stagnant,
        "count": int(warm) + int(humid) + int(stagnant),
    }
    if dollar_spot_pct is not None:
        dollar_spot = dollar_spot_pct >= t.dollar_spot_threshold_pct
        out["dollar_spot"] = dollar_spot
        out["dollar_spot_pct"] = round(dollar_spot_pct, 1)
        out["count"] = int(warm or dollar_spot) + int(humid) + int(stagnant)
    return out


def pressure_signals(w: WeatherReading, t: Tunables,
                     dollar_spot_pct: float | None = None) -> int:
    return pressure_breakdown(w, t, dollar_spot_pct)["count"]


def window_cap_minutes(w: WeatherReading, t: Tunables,
                       dollar_spot_pct: float | None = None) -> float:
    signals = pressure_signals(w, t, dollar_spot_pct)
    base = t.window_base_cap_hours
    disease = t.window_disease_cap_hours
    hours = base - (base - disease) * (signals / 3.0)
    return hours * 60.0
```

`pressure_agreement` (line ~150) reads only `warm`, `humid` and `stagnant`. Leave it unchanged.

- [ ] **Step 4:** Run the brain tests. Expected: all pass, including the existing ones.
- [ ] **Step 5:** Commit: `Count dollar spot risk in the disease-pressure signal`.

---

### Task 4: Engine cache and planning call sites

**Files:** Modify `CC/engine/base.py`, `CC/engine/planning.py`, `CC/engine/orchestration.py`, `tests/engine/test_planning_reads.py`, goldens.

- [ ] **Step 1: Write the failing tests.** Append to `tests/engine/test_planning_reads.py`:

```python
from custom_components.geodrops_rachio.brain.dollar_spot import DiseaseRisk

RISK = DiseaseRisk(pct=34.0, t5_c=15.5, rh5=88.0, days=5, as_of="2026-07-01")


def _fall_night(world):
    world.set("sensor.forecast_overnight_temp", "55")
    world.set("sensor.forecast_overnight_humidity", "95")


async def test_dollar_spot_shrinks_a_fall_window(freezer):
    world, eng = _eng(freezer)
    _fall_night(world)
    assert eng.project_window()["cap_hours"] == 5.0
    eng.disease_risk = RISK
    w = eng.project_window()
    assert w["pressure"] == ["dollar spot", "humid"]
    assert w["cap_hours"] == 4.0
    assert w["dollar_spot"] == {"pct": 34.0, "t5_c": 15.5, "rh5": 88.0, "days": 5}


async def test_dollar_spot_disabled_by_tunable(freezer):
    world, eng = _eng(freezer, entry_data(overrides="dollar_spot_enabled: false"))
    _fall_night(world)
    eng.disease_risk = RISK
    w = eng.project_window()
    assert w["cap_hours"] == 5.0 and w["dollar_spot"] is None


async def test_plan_cap_and_projection_agree_with_dollar_spot(freezer):
    world, eng = _eng(freezer)
    _fall_night(world)
    eng.disease_risk = RISK
    ctx = await eng._plan_context(eng._current_cfg)
    assert ctx["dollar_spot_pct"] == 34.0
    assert round(ctx["cap_minutes"] / 60.0, 2) == eng.project_window()["cap_hours"]
    pb, pb_instant = eng._pressure_pair(ctx)
    assert pb["dollar_spot"] is True and "dollar_spot" not in pb_instant
```

`_plan_context` is the method the existing `test_plan_context_reports_the_finish_controls` test calls. If the plan helper has a different name or needs zone data, follow that test's setup exactly. If `cap_minutes` is clamped by the remaining time at `T_PLAN`, compare to the projection only when unclamped; that test fixture plans at 23:00, so it should be unclamped.

- [ ] **Step 2:** Run `bash tools/test.sh tests/engine/test_planning_reads.py -q`. Expected: the new tests fail (`disease_risk` missing or keys absent).

- [ ] **Step 3: `engine/base.py`.** Add the type and the optional ctor argument (keyword-only, default `None`, so existing constructions keep working):

```python
FetchDailyMeans = Callable[
    [str, str, dt.date, dt.date],
    Awaitable[list[tuple[dt.date, float | None, float | None]]]]
```

```python
    def __init__(self, port: HAPort, store: EngineStore,
                 load_raw_config: Callable[[], dict[str, Any]],
                 fetch_zone_data: Callable[[], Awaitable[ZoneData]],
                 *, fetch_daily_means: FetchDailyMeans | None = None) -> None:
        ...
        self._fetch_daily_means_fn = fetch_daily_means
        # The latest dollar spot assessment (DiseaseRiskMixin refreshes it);
        # None = not active, and every output is as before dollar spot.
        self.disease_risk: DiseaseRisk | None = None
```

Import `DiseaseRisk` from `..brain.dollar_spot`. Add `import datetime as dt` if it is not already imported.

- [ ] **Step 4: `engine/planning.py`.**

1. Add a helper to `PlanningMixin`:

```python
    def _dollar_spot_pct(self, tun: config.Tunables) -> float | None:
        """Tonight's dollar spot probability for the pressure count, or None
        (disabled, no assessment, or outside the model's range): None leaves
        every breakdown and cap exactly as before dollar spot existed."""
        risk = self.disease_risk
        if not tun.dollar_spot_enabled or risk is None:
            return None
        return risk.pct
```

2. In the plan (lines ~650–662), compute `ds_pct = self._dollar_spot_pct(tun)` once, before the `forecast_wx` branch. Pass it to **both** `weather.window_cap_minutes(...)` calls. Add `"dollar_spot_pct": ds_pct,` to the returned ctx dict, after `"cap_source"`. That way the recap and records use the value the cap used, even if the tracker refreshes during the wait.

3. Replace `_pressure_pair`'s body:

```python
        tun = ctx["tun"]
        pb_instant = weather.pressure_breakdown(ctx["wx"], tun)
        # The first breakdown is the one that sized the cap, so it carries the
        # dollar spot value the cap used; the instantaneous one stays the
        # historical baseline the side-by-side comparison is for.
        ds_pct = ctx.get("dollar_spot_pct")
        driver = ctx["forecast_wx"] if ctx["forecast_wx"] is not None else ctx["wx"]
        if ctx["forecast_wx"] is None and ds_pct is None:
            return pb_instant, pb_instant
        return weather.pressure_breakdown(driver, tun, ds_pct), pb_instant
```

Update the docstring's first sentence to say the first breakdown is the one that sized the cap.

4. In `project_window` (lines ~395–424):
   - Compute `ds_pct = self._dollar_spot_pct(tun)`.
   - Pass it to `window_cap_minutes` and `pressure_breakdown`.
   - Change the `pressure` list to:

```python
            pressure = [name for name, on in (
                ("warm", flags["warm"]),
                ("dollar spot", flags.get("dollar_spot", False)),
                ("humid", flags["humid"]), ("still", flags["stagnant"])) if on]
```

   - Add to the returned dict:

```python
            "dollar_spot": None if ds_pct is None or self.disease_risk is None else {
                "pct": round(ds_pct, 1),
                "t5_c": round(self.disease_risk.t5_c, 1),
                "rh5": round(self.disease_risk.rh5, 1),
                "days": self.disease_risk.days},
```

   - The `reading is None` default path keeps `pressure = []` and the base cap, unchanged. Set `"dollar_spot"` there with the same expression.

- [ ] **Step 5: `engine/orchestration.py` `_plan_summary` (line ~707).** Make the active list:

```python
        active = [name for name, on in
                  (("warm", pb["warm"]),
                   (f"dollar spot {pb.get('dollar_spot_pct', 0):.0f}%",
                    pb.get("dollar_spot", False)),
                   ("humid", pb["humid"]), ("stagnant", pb["stagnant"]))
                  if on]
```

- [ ] **Step 6:** Run `bash tools/test.sh tests/engine -q`. Expected: the new tests pass. Golden tests may fail only on the new ctx key; regenerate with `GOLDEN_UPDATE=1 bash tools/test.sh tests/engine -q` and confirm `git diff tests/engine/golden` adds only `"dollar_spot_pct": null`. Anything else is a fail-open bug: fix the code, don't regenerate.
- [ ] **Step 7:** Run the full suite: `bash tools/test.sh -q`. Expected: all pass.
- [ ] **Step 8:** Commit: `Feed dollar spot risk into the plan, projection and recap`.

---

### Task 5: Refresh the cache — `engine/disease_risk.py` + Scheduler wiring

**Files:** Create `CC/engine/disease_risk.py` and `tests/engine/test_disease_risk.py`. Modify `CC/engine/scheduler.py`.

- [ ] **Step 1: Write the failing tests** in `tests/engine/test_disease_risk.py`:

```python
"""DiseaseRiskMixin: the hourly dollar spot refresh fails open and logs once."""
import datetime as dt
import logging

from custom_components.geodrops_rachio.engine.base import EngineBase
from custom_components.geodrops_rachio.engine.disease_risk import DiseaseRiskMixin
from custom_components.geodrops_rachio.engine.store import EngineStore
from custom_components.geodrops_rachio.config_writer import build_config
from tests.engine.scenario import T_PLAN, _nosave, entry_data, fake_fetch, populate
from tests.engine.world import FakePort, FakeWorld


def _eng(freezer, fetch, data=None):
    freezer.move_to(T_PLAN)
    world = FakeWorld(freezer)
    data = data or entry_data()
    populate(world, data)
    cls = type("TestEngine", (DiseaseRiskMixin, EngineBase), {})
    eng = cls(FakePort(world), EngineStore({}, _nosave),
              lambda: build_config(data), fake_fetch(world),
              fetch_daily_means=fetch)
    return world, eng


def _fetcher(t=20.0, rh=80.0, n=5, calls=None):
    async def fetch(temp_id, rh_id, first, end):
        if calls is not None:
            calls.append((temp_id, rh_id, first, end))
        return [(end - dt.timedelta(days=i), t, rh) for i in range(1, n + 1)]
    return fetch


async def test_refresh_reads_the_bound_sensors_for_five_days(freezer):
    calls = []
    _w, eng = _eng(freezer, _fetcher(calls=calls))
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is not None and round(eng.disease_risk.pct, 1) == 40.4
    temp_id, rh_id, first, end = calls[0]
    assert (temp_id, rh_id) == ("sensor.tempest_sensor_temperature",
                                "sensor.tempest_sensor_humidity")
    assert end == eng.port.now().date() and (end - first).days == 5


async def test_no_fetcher_or_too_few_days_is_none(freezer):
    _w, eng = _eng(freezer, None)
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None
    _w, eng = _eng(freezer, _fetcher(n=2))
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None


async def test_disabled_tunable_is_none_without_fetching(freezer):
    calls = []
    _w, eng = _eng(freezer, _fetcher(calls=calls),
                   entry_data(overrides="dollar_spot_enabled: false"))
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None and calls == []


async def test_fetch_error_fails_open_and_logs_once(freezer, caplog):
    async def boom(*_):
        raise RuntimeError("recorder not loaded")
    _w, eng = _eng(freezer, boom)
    with caplog.at_level(logging.INFO):
        await eng.async_refresh_disease_risk()
        await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None
    assert caplog.text.count("Dollar spot pressure is off") == 1


async def test_listeners_notified_only_on_change(freezer):
    _w, eng = _eng(freezer, _fetcher())
    seen = []
    eng.add_listener(lambda: seen.append(1))
    await eng.async_refresh_disease_risk()
    await eng.async_refresh_disease_risk()
    assert len(seen) == 1
```

- [ ] **Step 2:** Run `bash tools/test.sh tests/engine/test_disease_risk.py -q`. Expected: fails with `ModuleNotFoundError: ...engine.disease_risk`.

- [ ] **Step 3: Implement** `CC/engine/disease_risk.py`:

```python
"""Refreshes the dollar spot assessment the planner reads (self.disease_risk).

Fails open: anything short of a usable assessment leaves disease_risk None,
which keeps every pressure breakdown and cap exactly as before dollar spot.
A reason is logged once each time the signal turns off, and once when it
comes back, not on every hourly refresh.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..brain import dollar_spot
from .base import EngineBase

_LOGGER = logging.getLogger(__name__)


class DiseaseRiskMixin(EngineBase):
    _disease_risk_off_reason: str | None = None

    async def async_refresh_disease_risk(self) -> None:
        risk: dollar_spot.DiseaseRisk | None = None
        reason: str | None = None
        try:
            cfg = self._load_cfg()
        except Exception as err:
            cfg = None
            reason = f"the configuration does not load ({err!r})"
        fetch = self._fetch_daily_means_fn
        if cfg is not None:
            if not cfg.tunables.dollar_spot_enabled:
                reason = "dollar_spot_enabled is false"
            elif fetch is None:
                reason = "no statistics source"
            else:
                w = cfg.bindings.weather
                today = self.port.now().date()
                first = today - dt.timedelta(days=dollar_spot.WINDOW_DAYS)
                try:
                    daily = await fetch(w.temperature, w.humidity, first, today)
                except Exception as err:
                    reason = f"the recorder statistics cannot be read ({err!r})"
                else:
                    risk = dollar_spot.assess(daily, today)
                    if risk is None:
                        reason = (
                            f"fewer than {dollar_spot.MIN_DAYS} of the last "
                            f"{dollar_spot.WINDOW_DAYS} days have daily statistics "
                            f"for {w.temperature} and {w.humidity}")
        if reason is not None and reason != self._disease_risk_off_reason:
            _LOGGER.info("Dollar spot pressure is off: %s", reason)
        elif reason is None and self._disease_risk_off_reason is not None:
            _LOGGER.info("Dollar spot pressure is on again")
        self._disease_risk_off_reason = reason
        if risk != self.disease_risk:
            self.disease_risk = risk
            self._notify_listeners()
```

`_load_cfg` and `_notify_listeners` are both on `EngineBase` (`base.py:78`, `base.py:131`).

- [ ] **Step 4: Wire it into `engine/scheduler.py`.**
  - Add `DiseaseRiskMixin` to the `Scheduler` bases, before `EngineBase`, e.g. after `PlanningMixin`.
  - Add the keyword-only ctor arg `fetch_daily_means: FetchDailyMeans | None = None` and pass it to `super().__init__(..., fetch_daily_means=fetch_daily_means)`.
  - In `_startup_then_plan`, after `await self._on_startup()` and before `self.request_plan_refresh()`, add `await self.async_refresh_disease_risk()`. It runs after the safety check, so nothing delays it; a run started by recovery plans without it, which is the fail-open path.
  - In `_on_plan_time`, before `self.request_plan_refresh()`:

```python
        self._spawn_job(self.async_refresh_disease_risk(),
                        "geodrops_rachio_disease_risk")
```

  The plan refresh is debounced by `PLAN_REFRESH_DELAY_S` (5 s), so the refreshed value is normally in place for it. Either order is safe.

- [ ] **Step 5:** Run `bash tools/test.sh tests/engine -q`. Expected: all pass. Goldens are unchanged, because scenarios inject no fetcher.
- [ ] **Step 6:** Commit: `Refresh dollar spot risk hourly in the engine`.

---

### Task 6: Recorder statistics source — `disease_stats.py` + wiring

**Files:** Create `CC/disease_stats.py` and `tests/test_disease_stats.py`. Modify `CC/__init__.py` and `CC/manifest.json`.

- [ ] **Step 1: Write the failing tests** in `tests/test_disease_stats.py`. Patch the recorder so no database is needed:

```python
"""disease_stats reads daily means from recorder long-term statistics."""
import datetime as dt
from unittest.mock import patch

from homeassistant.util import dt as dt_util

from custom_components.geodrops_rachio import disease_stats


class _FakeRecorder:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _ts(day: dt.date) -> float:
    return dt_util.start_of_local_day(day).timestamp()


async def test_daily_means_per_local_day(hass):
    d1, d2 = dt.date(2026, 10, 13), dt.date(2026, 10, 14)
    rows = {
        "sensor.t": [{"start": _ts(d1), "mean": 15.0}, {"start": _ts(d2), "mean": 17.0}],
        "sensor.rh": [{"start": _ts(d1), "mean": 85.0}],
    }
    seen = {}

    def fake_stats(hass_, start, end, ids, period, units, types):
        seen.update(start=start, end=end, ids=ids, period=period,
                    units=units, types=types)
        return rows

    with patch.object(disease_stats, "get_instance", return_value=_FakeRecorder()), \
         patch.object(disease_stats, "statistics_during_period", fake_stats):
        out = await disease_stats.async_daily_means(
            hass, "sensor.t", "sensor.rh", dt.date(2026, 10, 10), dt.date(2026, 10, 15))
    assert out == [(d1, 15.0, 85.0), (d2, 17.0, None)]
    assert seen["ids"] == {"sensor.t", "sensor.rh"} and seen["period"] == "day"
    assert seen["units"] == {"temperature": "°C"} and seen["types"] == {"mean"}
    assert seen["start"] == dt_util.start_of_local_day(dt.date(2026, 10, 10))
    assert seen["end"] == dt_util.start_of_local_day(dt.date(2026, 10, 15))
```

- [ ] **Step 2:** Run `bash tools/test.sh tests/test_disease_stats.py -q`. Expected: fails with `ImportError`.

  Also confirm the recorder API in the test image before implementing. The positional order assumed below is `(hass, start_time, end_time, statistic_ids, period, units, types)`:

  ```bash
  docker run --rm geodrops-rachio-test python -c "import inspect; from homeassistant.components.recorder.statistics import statistics_during_period as s; print(inspect.signature(s))"
  ```

  If the order differs, pass keyword arguments in Step 3 and match `fake_stats` to them.

- [ ] **Step 3: Implement** `CC/disease_stats.py`:

```python
"""Daily mean temperature (C) and humidity (%) from recorder long-term
statistics, for the dollar spot assessment (engine/disease_risk.py).

Long-term statistics outlive the recorder's purge of raw states, start from the
history a user already has (no warm-up), and their daily mean is exactly the
model's input. A sensor without state_class: measurement has none; the engine
then turns the signal off and logs why.
"""
from __future__ import annotations

import datetime as dt

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def async_daily_means(
        hass: HomeAssistant, temp_id: str, rh_id: str,
        first_day: dt.date, end_day: dt.date,
) -> list[tuple[dt.date, float | None, float | None]]:
    """[(local day, mean C, mean %RH)] for first_day <= day < end_day, oldest
    first. A day missing one statistic carries None for it."""
    start = dt_util.start_of_local_day(first_day)
    end = dt_util.start_of_local_day(end_day)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {temp_id, rh_id}, "day",
        {"temperature": UnitOfTemperature.CELSIUS}, {"mean"})
    by_day: dict[dt.date, list[float | None]] = {}
    for col, stat_id in ((0, temp_id), (1, rh_id)):
        for row in stats.get(stat_id, []):
            day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
            by_day.setdefault(day, [None, None])[col] = row.get("mean")
    return [(day, v[0], v[1]) for day, v in sorted(by_day.items())]
```

- [ ] **Step 4: Wire it in `CC/__init__.py`.** Build the resolver once, use it for the port, and inject the fetcher:

```python
    resolve = owned_entities.resolver(hass, entry, renamed)
    scheduler = Scheduler(
        HassPort(hass, resolve), store,
        load_config,
        fetch_zone_data,
        lambda coro, name: entry.async_create_background_task(hass, coro, name),
        fetch_daily_means=lambda temp_id, rh_id, first, end: (
            disease_stats.async_daily_means(
                hass, resolve(temp_id), resolve(rh_id), first, end)))
```

Add `disease_stats` to the module's relative imports.

- [ ] **Step 5:** In `CC/manifest.json`, set `"after_dependencies": ["rachio", "recorder"]`. This is ordering only; the integration still loads when recorder is disabled, and the fetch error turns the signal off.
- [ ] **Step 6:** Run `bash tools/test.sh -q`. Expected: all pass, including `test_init.py` setup tests. If hassfest-style manifest tests exist, they accept `recorder` as an after-dependency.
- [ ] **Step 7:** Commit: `Read dollar spot inputs from recorder statistics`.

---

### Task 7: Watering window sensor attributes

**Files:** Modify `CC/sensor.py` and `tests/test_entities.py`.

- [ ] **Step 1: Write the failing test.** In `tests/test_entities.py`, beside `test_watering_window_follows_its_inputs`, add a test that:
  1. sets up the entry the same way;
  2. sets `entry.runtime_data.scheduler.disease_risk = DiseaseRisk(pct=34.0, t5_c=15.5, rh5=88.0, days=5, as_of="2026-10-14")`;
  3. calls `scheduler._notify_listeners()`;
  4. sets the forecast overnight temperature and humidity sensors to a fall night (55 / 95);
  5. asserts the window sensor's attributes include `disease_pressure == ["dollar spot", "humid"]`, `dollar_spot_pct == 34.0`, `dollar_spot_t5 == 15.5`, `dollar_spot_rh5 == 88.0` and `dollar_spot_days == 5`.

  Use the forecast sensor entity ids that test already sets.

  A second assertion, with `disease_risk = None`: the four `dollar_spot_*` attributes are present and `None`.

- [ ] **Step 2:** Run `bash tools/test.sh tests/test_entities.py -q -k watering_window`. Expected: the new test fails (`KeyError` / missing attributes).

- [ ] **Step 3: Implement.** In `WateringWindowSensor._update` (`sensor.py` ~line 541), add to the attributes dict after `"disease_pressure"`:

```python
                    # The Smith-Kerns dollar spot inputs behind a "dollar spot"
                    # pressure entry (None when the signal is off).
                    "dollar_spot_pct": (w["dollar_spot"] or {}).get("pct"),
                    "dollar_spot_t5": (w["dollar_spot"] or {}).get("t5_c"),
                    "dollar_spot_rh5": (w["dollar_spot"] or {}).get("rh5"),
                    "dollar_spot_days": (w["dollar_spot"] or {}).get("days"),
```

The sensor already re-renders on `scheduler.add_listener`, and the tracker notifies on change, so no new subscription is needed.

- [ ] **Step 4:** Run `bash tools/test.sh -q`. Expected: all pass.
- [ ] **Step 5:** Commit: `Show dollar spot inputs on the Watering window sensor`.

---

### Task 8: Verify end to end and open the PR

- [ ] **Step 1:** Run the full suite once more: `bash tools/test.sh -q` and the brain tests natively. Expected: all pass.
- [ ] **Step 2:** Run the repo's lint/type checks the same way CI does. Check `.github/workflows/` for the exact commands, e.g. `ruff` / `mypy`, and fix any findings.
- [ ] **Step 3:** Confirm the golden diff across the branch (`git diff main -- tests/engine/golden`) contains only the two tunable fields and `dollar_spot_pct: null`.
- [ ] **Step 4:** Push the branch and open a PR to `main`, titled `Dollar spot (Smith-Kerns) disease pressure`. The body summarizes:
  - the gap (the 68 °F flag misses fall and spring diseases);
  - the model and its source;
  - fail-open behaviour;
  - the two tunables;
  - that no CHANGELOG entry is included (the release PR adds it).

  End the body with the attribution line from the session.
- [ ] **Step 5:** Bind the PR with the `ccd_pr` tools and report CI. Do not merge or release; releases go out as a beta via `tools/release.sh` from the main checkout, at the user's call.

## Self-review notes (for the implementer)

- **Spec coverage:**
  - Spec §1 → Task 1.
  - Spec §2 → Task 6.
  - Spec §3 → Task 5.
  - Spec §4 → Tasks 3–4.
  - Spec §5 → Tasks 4 and 7.
  - Spec §6 → Task 2.
  - The spec's testing list is spread across the same tasks.
- **Two plan-time choices** go beyond the spec's wording and keep it consistent:
  - the cap's `dollar_spot_pct` is stored in the ctx, so the recap matches the cap even if the tracker refreshes during the wait;
  - `_pressure_pair` uses the instantaneous reading as the cap driver when the forecast is missing.
