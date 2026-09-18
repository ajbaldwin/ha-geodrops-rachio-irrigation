# Changelog

Notable changes to the GeoDrops + Rachio Irrigation integration. HACS shows
each release's notes, so entries here stay user-facing and concise — one
section per released version, newest first.

## v0.9.9 — Smarter calibration settle timing

### Changes
- Self-calibration now reads the settled soil moisture on a frequent poll and
  only accepts a reading once the sensor has genuinely reported *after* the
  settle time — instead of once at a fixed hour. This stops a late or missed
  GeoDrops check-in (the sensors report only every few hours, and can skip one)
  from being mistaken for "no moisture rise" and wrongly rejecting a good
  calibration probe. If no fresh reading arrives in time, the observation is
  dropped as inconclusive rather than rejected, so a sensor gap can never
  corrupt a zone's learned calibration.

Brain-only update (bundled scheduler v0.8.4) — **no Home Assistant restart
required**; HACS applies it on the next update.

## v0.9.8 — Per-zone Deficit sensor

### Changes
- Each zone now has a **Deficit** sensor (moisture %) — how far current soil
  moisture sits below the zone's need-water target. It reads 0 at or above
  target and updates live as the soil dries. The scheduler now publishes target
  floors at startup, so the sensor has a value right after a restart rather than
  only after the first nightly plan.
- **Planned Runtime** now keeps its value across a restart instead of briefly
  reading unknown.

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.7 — Exclude toggle survives restarts; tidier zone editing

### Fixes
- A zone's **Exclude from watering** toggle now reliably keeps its on/off state
  across a Home Assistant restart.
- When adding a zone whose key clashes with an existing one, the form now keeps
  everything else you already typed instead of clearing the fields.

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.6 — Refill in mm, tidier efficacy, richer calibration status

### Fixes
- **Refill depth** now reads correctly in millimetres. It was showing **0** on
  installs using imperial units, because the sensor's distance class made Home
  Assistant convert the small value to inches and round it to zero.

### Changes
- **Efficacy** now displays at most 3 decimal places instead of a long number.
- **Calibration State** now tells you more at a glance: probe progress toward
  convergence (e.g. *Calibrating (2/3)*) and, when a zone is stuck, why
  (*Calibrating — soil too wet* / *probe too small* / *rained out*).

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.5 — Per-zone refill depth sensor

### Changes
- Each zone now has a **Refill depth** sensor (mm) — Rachio's "depth of water"
  for the zone, the amount it needs from depletion back to field capacity. It
  shows the value captured at setup and updates to Rachio's live value whenever
  you press **Refresh Runtimes**.

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.4 — Readable status labels

### Changes
- The **Status** sensor and per-zone **Calibration State** now display
  capitalized labels (*Idle*, *Watering*, *Waiting*, *Calibrating*, *Converged*,
  …) instead of the raw lowercase tokens.

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.3 — Accurate Last Watered time, and Preview works before dawn

### Fixes
- **Last Watered** now shows the real time watering finished (valve close),
  not the time the nightly plan was published. The bundled scheduler now
  records a full timestamp for the end of watering, and this integration reads
  it.
- **Preview** now works while a night is *planned and waiting* for its pre-dawn
  window — previously it returned "skipped, run in progress" for the hours
  between planning and watering. Preview is still declined while valves are
  actually watering.

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.2 — Run status, calibration state, and last-watered fixes

### Changes
- New **Status** sensor on the main (Irrigation Controls) device showing what
  the scheduler is doing — idle, planning, waiting, watering, standby, skipped,
  or aborted — with a detail attribute.

### Fixes
- **Calibration State** now reports the live per-zone state (e.g. *calibrating*)
  instead of reading *unknown*.
- **Last Watered** now populates for zones that ran, instead of staying blank.
  (It reflects the nightly run's timestamp; exact valve-close time is a later
  change.)

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.1 — Friendlier zone names and sensor units

### Changes
- Zone devices now show a readable name (e.g. **Front Slope**) instead of the
  raw zone key (`front_slope`). Entity IDs are unchanged, and a device you
  renamed in the UI keeps your name.
- The per-zone **Soil moisture** sensor now reports a percentage with a
  moisture icon (e.g. **75.6 %**) instead of a bare number, and the
  **Efficacy** sensor carries a `%/min` unit (moisture gained per watering
  minute).

Updating requires a Home Assistant restart (the integration ships Python).

## v0.9.0 — Clearer device name; version aligned with the scheduler line

The version jumps from 0.5.0 to 0.9.0 so the integration's releases continue
the line of the standalone GeoDrops + Rachio scheduler it succeeds — that
scheduler is now vendored inside this integration rather than installed
separately, so there is a single release stream going forward.

### Changes
- The integration's site-level device is now named **Irrigation Controls**
  instead of "GeoDrops + Rachio Irrigation", which duplicated the integration's
  own name and looked like two identical entries. Zone devices are unchanged,
  and no entity IDs change. If you already renamed this device yourself, your
  name is kept.

Updating requires a Home Assistant restart (the integration ships Python).

## v0.5.0 — Initial public release

First public, HACS-installable release. Wraps the GeoDrops + Rachio pyscript
irrigation scheduler in a UI-configurable Home Assistant integration — no YAML
to hand-edit, no restart to complete setup.

### Features
- Setup wizard with Rachio auto-discovery (controllers and zones)
- Per-zone devices, each with an exclude switch and status sensors: soil
  moisture, planned / last-delivered runtime, last watered, efficacy,
  calibration state
- Manage-zones flow (add / edit / delete a zone)
- Native controls: Stop, Run Now, Preview, Reset and Refresh Runtimes buttons;
  a drought-level select; run-active / standby / dew-formed switches
- Observed and forecast overnight weather sensors (temperature, humidity, wind)
- Active Watering Calibration toggle and advanced tunable overrides, including
  per-drought-level overrides such as the watering-window anchor offsets
- Bundled scheduler brain delivered and updated without a Home Assistant
  restart; local brand icon

### Fixes
- Stop button no longer aborts a live run across a restart or `pyscript.reload`
- Overnight weather window follows the home's timezone instead of UTC

Updating from an earlier manual install requires a Home Assistant restart
(the integration ships Python).
