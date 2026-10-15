# Auto Watering Time (Frost Shift) — Design

Date: 2026-10-15
Status: PARKED (2026-10-15) — split out; dollar spot pressure ships first
Related: [Dollar spot disease pressure](2026-10-15-dollar-spot-pressure-design.md)
Scope: `brain/config.py`, new `brain/frost.py`, `engine/planning.py`, `engine/orchestration.py`,
`engine/scheduler.py`, `switch.py`, `sensor.py`, `weather_derive.py`,
`brain/report_format.py`, `strings.json`, `translations/en.json`, README, tests.

## Problem

**Timing is static.** The window ends at a sun event (dawn or sunrise, per
drought level, or forced by the Finish anchor select) minus an offset.
Nothing reacts to cold mornings. In late fall and early spring a pre-dawn
run can water near-freezing turf, glaze walks and driveways, and run the
system in conditions that damage it.

## Agronomic basis (what the design must respect)

- **Water during the natural dew period, early morning, in every season.**
  Sources: UF/IFAS LH040, NC State, Missouri IPM1029, Virginia Tech, and
  Purdue BP-218-W (2024: early-morning watering for brown patch, dollar
  spot, red thread and gray leaf spot).
- **Never water in the evening.** Purdue (Latin, 2016) names 6–9 pm as the
  worst time.
- **After the dew dries, watering extends leaf wetness** (UF/IFAS).
  Purdue AY-7 still rates 8 am–noon as second best, so a short delay past
  sunrise is acceptable; a mid-morning default is not.
- **Don't water near freezing.** Oklahoma State: not below 40 °F (ice on
  plants and walks). UGA: not at ≤ 32 °F (system damage).

The principle: **finish at the sun anchor as today; on cold mornings, slide
later just enough to clear the frost, bounded by a short cap after
sunrise; skip if it cannot fit.** Late finishes stay rare and short.

## Goals

- An **Auto watering time** switch. On: the system picks the finish time
  (level-default anchor, frost shift). Off (manual): today's behaviour,
  driven by the Finish anchor select and Finish offset slider.
- A **frost shift** in auto mode, from the hourly forecast.
- A **live frost backstop** at window start in both modes.

## Non-goals

- Disease pressure changes (see the dollar spot spec).
- No fixed clock-time anchor for manual mode (possible follow-up).
- No change to `run_now` (an explicit, immediate user action), rain/hail
  abort, dosing, or calibration settle timing.

## Design

### 1. Mode switch

New restored switch `switch.geodrops_rachio_auto_timing` ("Automatic
watering time"), `EntityCategory.CONFIG`, **default off**. Off keeps
upgrades behaviour-identical, apart from the live backstop (§4). It is
added via `_FLAGS`-style construction in `switch.py`, and its translation
key goes in `strings.json` / `en.json`.

New `HABindings.auto_timing_switch` defaults to the entity id. This
mirrors `finish_anchor_select`: it is not a wizard binding, and a missing
entity reads as off.

| Mode | End anchor | End offset | Frost shift |
|---|---|---|---|
| Manual (off) | `finish_anchor(profile, select)` (today) | profile offset + slider (today) | no |
| Auto (on) | `profile.end_anchor` (select ignored) | profile offset (slider ignored) | yes |

Rename the Finish anchor select's `auto` option **label** to "Level
default" (translation only; the state key stays `auto`, so no migration).
This removes the clash with "auto mode".

### 2. Hourly morning forecast input

New diagnostic sensor `sensor.geodrops_rachio_forecast_morning_temperature`.
It uses the same machinery as `ForecastOvernightSensor`: hourly refresh,
retries, unit conversion.

- **State:** the minimum forecast temperature over the frost search span,
  in the scheduler's unit (°F internally, displayed per HA).
- **Attribute `hourly`:** `[[iso_start, temp_f], …]` for each forecast hour
  from 00:00 to 12:00 of the coming morning (`weather_derive.morning_span(now)`;
  after 12:00 it targets tomorrow).
- **Binding:** `derived.forecast_morning_temp`.

Attributes are excluded from the recorder (`_unrecorded_attributes`), like
the existing large-attribute sensor.

### 3. Frost shift (auto mode, nightly plan) — `brain/frost.py`

Pure function:

```python
def frost_shift(hourly: list[tuple[datetime, float]],  # hour start, °F
                base_end: datetime,      # anchor − offset, as today
                sunrise: datetime,
                span_minutes: float,     # minutes the built plan needs
                cap_minutes: float,      # disease-window cap
                tun: Tunables) -> FrostDecision
```

`FrostDecision(end, cap_minutes, clear_at, shift_minutes, skip: bool, reason)`.

Each forecast hour's temperature holds for that hour. "Cold" means
< `frost_temp_f`.

1. `latest_end = sunrise + frost_max_delay_minutes`. The cap is always
   measured from sunrise, even at dawn-anchored levels.
2. Let the run occupy `[base_end − span, base_end]`. If no cold hour
   overlaps it, return `shift_minutes = 0` (no change).
3. Else `clear_at` = the start of the first hour from which every hour up
   to `latest_end` is warm. If `clear_at ≥ latest_end` (cold through the
   cap), return `skip=True, reason="frost"`.
4. `end = clamp(clear_at + span, base_end, latest_end)`.
5. `cap = min(cap_minutes, end − clear_at)`. The planner re-runs
   `build_plan` with this cap, and lower-priority zones drop as
   `insufficient window` through the existing path. If the rebuilt plan
   waters nothing (`build_plan` returns `watered=[]`), skip with
   `reason="frost"`.
6. Missing or unusable `hourly` returns no shift. The live backstop (§4)
   covers that case.

`sunrise` is read once at plan time and kept in the ctx.
`sensor.sun_next_rising` rolls over to the next day once the sun is up,
so nothing after planning may re-read it for this run.

**Planning integration (`planning.py`, after `_window_end`).** Applies only
when auto mode is on and the trigger is the nightly:

1. Build the plan as today. If the frost decision changes `end` or `cap`,
   rebuild with the new cap and set `start = end − span` and
   `earliest_start = end − cap`.
2. On skip, the nightly takes the existing no-water path with skip reason
   `frost-forecast`.
3. Add `frost` to the ctx (`shift_minutes`, `clear_at`, `reason`), the
   last-run record and the recap (`report_format`).

The `remaining` clamp stays as is. It already handles a mid-window restart,
and recovery re-plans through `_plan`, so the shift is recomputed from the
newest forecast.

`project_window` applies the same function, so the Watering window sensor
shows the shifted time ahead of the run. New attributes: `timing_mode`
(`auto` | `manual`), `frost_shift_minutes`, `frost_clear_at`, and
`frost_skip`.

### 4. Live frost backstop (both modes, nightly only)

The window-start re-check (`orchestration.py`, after the wait, beside the
rain and moisture re-checks) reads the live `wx.temp_f`. If it is below
`frost_skip_temp_f` (default 34 °F), skip with reason `frost-at-window-start`.

This is a hazard guard, not a timing choice, so it also applies in manual
mode. It catches a wrong forecast and the no-forecast case. Set
`frost_skip_temp_f` to a very low value to disable it.

### 5. Tunables (`Tunables`, README table, example YAML)

| Tunable | Default | Meaning |
|---|---|---|
| `frost_temp_f` | 38.0 | Forecast hour below this is "cold" (auto shift). |
| `frost_max_delay_minutes` | 90 | Latest finish = sunrise + this. |
| `frost_skip_temp_f` | 34.0 | Live temp at window start below this skips (both modes). |

## Testing

**`tests_brain/test_frost.py`.** Fixture: sunrise 07:30, `base_end` 07:30
(offset 0), max delay 90, so `latest_end` = 09:00.

- No cold hours → no shift.
- Cold until 06:00, span 60 (run 06:30–07:30) → no shift.
- Cold until 08:00, span 30 → end 08:30, shift 60.
- Cold until 08:00, span 120 → end 09:00 (= latest), cap 60 → rebuild.
- Cold through 09:00 → skip.
- Empty or garbled hourly → no shift.
- Dawn-anchored level (`base_end` 07:00) still caps at sunrise + 90.

**Engine tests:**

- Auto off ignores the frost shift but obeys the live backstop.
- Auto on ignores the select and slider.
- `frost-forecast` and `frost-at-window-start` skip paths publish the
  last run and recap.
- `project_window` carries the frost attributes.

Run via `bash tools/test.sh`.

## Risks and open points

- **Calibration settle after a shifted finish.** It lands up to ~90 min
  later in the afternoon (more ET before the 6 h reading). This only
  happens on cold mornings, when ET is low. Accept; revisit if flagged
  samples rise.
- **Disease-cap averaging window.** It still uses the 20:00–06:00 forecast
  means, which a shifted run partly falls outside. Frost nights carry low
  disease pressure, so accept.
- **Mower and municipal-hours conflicts.** The default 90 min cap keeps
  finishes ≈ 09:00–09:30 at US latitudes. Users with a 10:00 rule may need
  to lower it.
