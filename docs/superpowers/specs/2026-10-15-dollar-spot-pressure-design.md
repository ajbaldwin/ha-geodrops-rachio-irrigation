# Dollar Spot Disease Pressure — Design

Date: 2026-10-15
Status: Approved for planning
Scope: new `brain/dollar_spot.py`, `brain/weather.py`, `brain/config.py`,
new `engine/disease_risk.py`, `engine/planning.py`, `engine/scheduler.py`,
`brain/report_format.py`, `sensor.py` (Watering window attributes),
`manifest.json`, README, tests.
Related: [Auto watering time (parked)](2026-10-15-auto-watering-time-design.md)

## Problem

The disease-window cap shrinks from 6 h to 3 h as three overnight flags
switch on: `warm` (> 68 °F), `humid` (> 90 % RH) and `still` (< 2 mph)
(`brain/weather.py:27`).

The 68 °F `warm` flag matches brown patch (Purdue's night ≥ 68 °F rule). But
it is off all spring and fall, which is exactly when dollar spot (April to
early November, 60–90 °F) and red thread (September–October, cool with heavy
dew) are active. A single night's reading also can't tell one humid night
from five in a row, and that run of days is what drives a dollar spot
epidemic.

## Agronomic basis

- **The Smith-Kerns dollar spot model** (UW–Madison Turfgrass Diagnostic
  Lab) is the model behind Purdue's Turfgrass Disease Prediction Tool:
  `logit = −11.4041 + 0.0894·RH₅ + 0.1932·T₅(°C)`,
  `P = e^logit / (1 + e^logit)`.
  RH₅ and T₅ are 5-day moving averages of the daily mean relative humidity
  and air temperature. The model is valid only for 10 °C ≤ T₅ ≤ 35 °C.
  Purdue and UW use 20 % as the action threshold.
- **Purdue BP-218-W (2024):** dollar spot hits Kentucky bluegrass and most
  species except tall fescue. Brown patch mainly hits tall fescue. Both
  are managed by limiting leaf-wetness duration (early-morning watering).
- **Caveat:** the model was validated on creeping bentgrass golf turf, and
  the 20 % threshold comes from fungicide timing. Treat it as a tunable
  starting point for lawns.

## Goals

- Compute the dollar spot probability locally, from the bound temperature
  and humidity sensors.
- Feed it into the existing disease-window cap, so fall and spring dew
  periods shrink the window the way summer heat already does.
- Work for **any grass mix**, with no grass-type setting.
- Make it visible where pressure is already shown.

## Non-goals

- **No dedicated dollar spot sensor.** It is visible through the existing
  Watering window sensor and run records only. It could be added later from
  the same cache.
- No grass-type setting.
- No change to the finish time, frost handling, or the timing mode. Those
  belong to the parked auto-timing spec.
- No use of Purdue's online tool (Indiana only, no API).
- No change to the forecast-vs-observed calibration instrument.

## Design

### 1. Pure model — `brain/dollar_spot.py`

```python
def probability_pct(t5_c: float, rh5: float) -> float | None:
    """Smith-Kerns probability in percent; None outside 10–35 °C
    (the model is not valid there, so dollar spot is not active)."""

def five_day_means(daily: list[tuple[date, float | None, float | None]],
                   today: date) -> tuple[float, float, int] | None:
    """(T₅ °C, RH₅ %, days used) from the most recent 5 COMPLETE local days
    before `today`. None if fewer than 4 of those days have both values."""
```

Using complete days only keeps the value stable through the night: the
23:00 plan and a restart at 02:00 see the same number.

### 2. Data source — recorder long-term statistics

The daily `mean` of the bound `weather.temperature` and `weather.humidity`
sensors, read via
`recorder.statistics.statistics_during_period(hass, start, end,
{temp_id, rh_id}, period="day", units=None, types={"mean"})` on the
recorder's executor (`get_instance(hass).async_add_executor_job`).

- **Why this source:**
  - No new persistence.
  - Works immediately from existing history (no 5-day warm-up).
  - Survives restarts.
  - Daily means are exactly the model's input.
- **Temperature conversion:** from the statistic's metadata unit to °C.
- **Manifest:** add `recorder` to `after_dependencies`.

### 3. Tracker — `engine/disease_risk.py`

`DiseaseRiskTracker` belongs to the scheduler. It is refreshed at startup
(after `async_at_started`) and on the existing hourly `_on_plan_time` tick
(`scheduler.py:507`).

- **Cache:** `DiseaseRisk(pct, t5_c, rh5, days, as_of)` or `None`.
- **Sync access:** planning and `project_window` read the cache
  synchronously. Data that is up to an hour old is fine for daily means.
- **Fail open:** `None` (no statistics, a sensor without
  `state_class: measurement`, recorder excluded, query error, or
  `dollar_spot_enabled: false`) means "not active". The cap then behaves
  exactly as today. A single INFO log per transition into and out of
  `None` says why.

### 4. Feeding the cap — `brain/weather.py`

The temperature signal becomes "temperature favours disease":

```
dollar_spot = pct is not None and pct >= tun.dollar_spot_threshold_pct
temp_signal = warm or dollar_spot
count       = temp_signal + humid + stagnant      # still 0..3
```

- **Signatures:** `pressure_breakdown(w, t, dollar_spot_pct=None)` and
  `window_cap_minutes(w, t, dollar_spot_pct=None)`. Defaulting to `None`
  leaves existing callers unchanged.
- **`PressureBreakdown` gains:** `dollar_spot: bool` and
  `dollar_spot_pct: float | None`.
- **The divisor stays 3,** so summer is unchanged: when `warm` is already
  on, dollar spot adds nothing.

**Why "or" fits any grass mix:**

- The 68 °F rule covers brown patch (tall fescue).
- Smith-Kerns covers dollar spot (Kentucky bluegrass, ryegrass, fine
  fescue, and most blends).
- A false positive on pure tall fescue only shortens the window, which
  still ends at the anchor. It costs no water and does no harm to the turf.

**Call sites (`engine/planning.py`):**

- `_plan` (the cap at line ~659/662) passes the tracker's `pct` to
  `window_cap_minutes`.
- `project_window` (line ~410) passes it too, so the projection and the
  plan agree.
- `_pressure_pair` passes it to the **forecast** breakdown only. The
  instantaneous breakdown stays the historical baseline, so the side-by-side
  comparison keeps isolating the forecast change.
- `learning.py` (forecast-vs-observed calibration) is untouched. The
  dollar spot value is identical on both sides, so it adds no information
  there.

### 5. Visibility

**Watering window sensor:**

- The `pressure` list gains the label `"dollar spot"` when the signal is on.
- New attributes: `dollar_spot_pct` (rounded to 0.1), `dollar_spot_t5`
  (°C, rounded to 0.1), `dollar_spot_rh5`, and `dollar_spot_days`. All are
  `None` when unavailable.

**Last-run record and recap:** the ctx and last-run record carry the
breakdown. `report_format` mentions `dollar spot NN%` beside the other
pressure flags when it is active.

### 6. Tunables (`Tunables`, README table, example YAML)

| Tunable | Default | Meaning |
|---|---|---|
| `dollar_spot_enabled` | true | Feed Smith-Kerns into the disease-window cap. |
| `dollar_spot_threshold_pct` | 20.0 | Probability at which dollar spot counts as pressure. |

## Testing

**`tests_brain/test_dollar_spot.py`:**

- `probability_pct(20, 80)` = 40.4 and `(15, 75)` = 14.2. Both were
  checked numerically.
- `None` at T₅ = 9.9 °C and 35.1 °C; a value at exactly 10 and 35.
- `five_day_means`: 5 days, 4 days (one gap), 3 days → `None`; today's
  partial day excluded; days with only one value don't count.

**`tests_brain/test_weather.py`:**

- The dollar spot flag lifts the count only when `warm` is off.
- At or above the threshold it counts; below it doesn't.
- `None` gives today's counts and caps exactly. That is a regression guard
  on the existing tests.

**Engine tests (`tests/`):**

- The tracker converts °F statistics to °C.
- Missing statistics → `None`, logged once.
- `dollar_spot_enabled: false` → `None`.
- The cap with the tracker active is smaller than without, on a fall-like
  fixture (55 °F, 92 % RH).
- `project_window` and `_plan` agree on the cap.
- The Watering window sensor attributes are present and the `pressure`
  label appears.

Run the suite with `bash tools/test.sh`.

## Risks

- **Validation scope:** it was validated on golf turf. The threshold is
  tunable, and the effect is bounded: it only shrinks the window (moves the
  start later), never the finish.
- **Statistics availability** depends on the bound sensors having
  `state_class: measurement`. Tempest's do; others fail open, with a
  logged reason.
- **Recorder retention:** long-term statistics are kept indefinitely by
  default, so purging short-term states does not affect them.
