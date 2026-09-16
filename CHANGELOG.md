# Changelog

Notable changes to the GeoDrops + Rachio Irrigation integration. HACS shows
each release's notes, so entries here stay user-facing and concise — one
section per released version, newest first.

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
