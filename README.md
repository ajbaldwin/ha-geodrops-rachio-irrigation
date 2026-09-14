# GeoDrops + Rachio Irrigation

A Home Assistant integration that runs a **soil-moisture-driven irrigation
scheduler for a Rachio controller**, using **GeoDrops soil-moisture sensors**
as its input.

> **This is not a generic sprinkler/irrigation integration.** It is a
> purpose-built wrapper around one scheduler ("the brain") that expects
> Rachio hardware for zone control and GeoDrops sensors for soil-moisture
> readings. If you don't have both, this integration has nothing to bind to
> and won't be useful to you.

The integration itself does not talk to Rachio or GeoDrops directly. It
delivers a versioned copy of the scheduler as a [pyscript](https://github.com/custom-components/pyscript)
app, generates its YAML configuration from a HA config-flow wizard, and keeps
both in sync as you install updates.

## Prerequisites

Before installing this integration, you need all of the following already
set up and working in Home Assistant:

- **[HACS](https://hacs.xyz/)** — used to install this integration and to
  deliver its updates.
- **[pyscript](https://hacs-pyscript.readthedocs.io/)**, installed via HACS
  and configured/running. This integration does **not** install or enable
  pyscript for you — it delivers the scheduler as a pyscript app and asks
  the already-running pyscript integration to reload, via the
  `pyscript.reload` service. If pyscript isn't set up first, setup aborts.
- The **Rachio** integration, configured with your Rachio controller and
  zones.
- **GeoDrops soil-moisture sensors** already flowing into Home Assistant as
  entities (one dominant sensor and quality/agreement sensors per zone you
  want to schedule).
- A local weather station — a **Tempest** station or equivalent — exposing
  temperature, humidity, wind speed, rain-in-the-last-hour, and
  precipitation-type sensors.
- A forecast `weather.*` entity (any HA weather platform that provides
  forecasts), used for rain-skip and drought-level decisions.
- Home Assistant **2026.3.0** or newer (the Python 3.14 era of HA core).

Setup checks for pyscript and Rachio being loaded and will abort with
"Install and set up pyscript and the Rachio integration first" if either is
missing.

## Installing

1. In HACS, add this repository as a **custom repository** (category:
   *Integration*).
2. Install **GeoDrops + Rachio Irrigation** from HACS.
3. Go to **Settings → Devices & Services → Add Integration**, search for
   **"GeoDrops + Rachio Irrigation"**, and complete the setup wizard:
   - **Core bindings** — your notification service, calendar, Rachio device
     name/API key secret, standby switch, and forecast entity (all picked
     from entity selectors, not typed by hand).
   - **Weather station** — your local temperature/humidity/wind/rain/precip
     sensors, plus the sensor-name prefixes used for per-zone precipitation
     forecasts.
   - **Zones** — add one or more zones, each pointing at its Rachio switch,
     dominant moisture sensor, state sensor, quality sensors, target
     moisture range, runtime, and refill depth.
   - **Advanced** — optional YAML tunable overrides, and the Active Watering
     Calibration toggle (see below). The override YAML is a mapping merged over
     the scheduler defaults: top-level keys set `tunables`, and a
     `drought_profiles:` key deep-merges per drought level (each level keeps the
     defaults you do not mention). For example, to end the watering window later
     into the morning at wetter drought levels:

     ```yaml
     cycle_minutes: 12
     humid_rh_pct: 90
     drought_profiles:
       "Level 0 - Normal": {end_offset_minutes: -60}
       "Level 1 - Mild": {end_offset_minutes: -30}
       "Level 2 - Significant": {end_offset_minutes: -15}
     ```

There is **no YAML file to hand-edit and no Home Assistant restart required**
to complete installation. The wizard's answers are written straight into a
generated pyscript config file, the scheduler app is copied into your
pyscript apps directory, and the integration calls `pyscript.reload` to load
it — because pyscript is already running, it just picks the new app up. You
will not find (and do not need to add) a `pyscript: apps:` entry for this
integration anywhere in your YAML configuration.

Only one instance of this integration may be configured at a time; adding a
second one is blocked. To change bindings, weather sensors, zones, or the
advanced settings later, use the integration's **Configure** option — it
re-runs the same wizard, pre-filled with your current settings.

## Active Watering Calibration (Beta)

The **Active Watering Calibration** option in the Advanced step of the
wizard is **Beta and off by default** (`self_calibration_enabled`). Leave it
off unless you specifically want to try it; it is not required for the
scheduler's normal drought-level/soil-moisture-driven watering to work.

## Updates

Update this integration through HACS the same way you update any other
custom integration. What happens next depends on what changed in that
release:

- If the release only updates the **scheduler brain** (the vendored
  scheduler app and its library), the integration notices the change on its
  own HACS update entity, reloads its config entry, and pushes the new
  scheduler code into pyscript with a live `pyscript.reload` call —
  **no Home Assistant restart needed**.
- If the release changes the **integration's own Python code** (the config
  flow, entities, delivery, or update logic), Home Assistant needs to
  re-import that code, which only happens on restart — HACS will flag this
  update as requiring a restart, the same as it would for any other custom
  integration.

## For maintainers

See [`docs/RELEASING.md`](docs/RELEASING.md) for how the scheduler brain is
vendored into this repo and how to cut a release.
