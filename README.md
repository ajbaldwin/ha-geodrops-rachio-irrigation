# GeoDrops + Rachio Irrigation

A Home Assistant integration that runs a **soil-moisture-driven irrigation
scheduler for a Rachio controller**, using **GeoDrops soil-moisture sensors**
as its input.

> **This is not a generic sprinkler/irrigation integration.** It is a
> purpose-built wrapper around one scheduler ("the brain") that expects
> Rachio hardware for zone control and GeoDrops sensors for soil-moisture
> readings. If you don't have both, this integration has nothing to bind to
> and won't be useful to you.

The scheduler ("the brain") runs natively inside this integration — there is
no separate app to deliver or reload. A HA config-flow wizard collects your
bindings and generates the scheduler's settings directly from your answers.

## Prerequisites

Before installing this integration, you need all of the following already
set up and working in Home Assistant:

- **[HACS](https://hacs.xyz/)** — used to install this integration and to
  deliver its updates.
- The **[Rachio](https://www.home-assistant.io/integrations/rachio/)**
  integration, configured with your Rachio controller and zones.
- **GeoDrops soil-moisture sensors** already flowing into Home Assistant as
  entities (one dominant sensor and quality/agreement sensors per zone you
  want to schedule). The simplest way to get these into Home Assistant is
  **[ha-geodrops-hacs](https://github.com/ajbaldwin/ha-geodrops-hacs)**, a
  native HACS integration that reads GeoDrops readings straight from BigQuery
  with no external service to run. An off-box alternative is
  **[ha-geodrops-integration](https://github.com/ajbaldwin/ha-geodrops-integration)**,
  which syncs the same readings to Home Assistant over MQTT.
- Weather inputs for the five observed conditions the wizard binds:
  temperature, humidity, wind speed, rain-in-the-last-hour, and
  precipitation-type. A **local weather station (a Tempest or equivalent) is
  strongly recommended** — it reads your yard's own microclimate at minute
  resolution and exposes all five natively. **Without a local station you can
  still run at reduced accuracy** by binding these to a public weather
  integration's sensors (e.g. OpenWeatherMap, Pirate Weather) or templating
  them from a `weather.*` entity's attributes. Rain-in-the-last-hour and
  precipitation-type are the fields public providers are least likely to
  expose cleanly, so they may need a template sensor.
- A forecast `weather.*` entity (any HA weather platform that provides
  forecasts, including free public ones such as Met.no), used for rain-skip
  and drought-level decisions.
- Home Assistant **2026.3.0** or newer (the Python 3.14 era of HA core).

Setup checks for the Rachio integration being loaded and will abort with
"Install and set up the Rachio integration first" if it is missing.

## Installing

1. In HACS, add this repository as a **custom repository** (category:
   *Integration*): HACS → ⋮ → *Custom repositories* → paste
   `https://github.com/ajbaldwin/ha-geodrops-rachio-irrigation`.
2. Install **GeoDrops + Rachio Irrigation** from HACS, then restart Home
   Assistant.
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
to complete installation (beyond the one restart in step 2, needed to load the
integration's own code, same as any other HACS integration). The wizard's
answers are written straight into the config entry, and the integration
reloads itself to pick them up — the scheduler runs natively inside the
integration, so there's no separate app or reload service involved.

Only one instance of this integration may be configured at a time; adding a
second one is blocked. To change bindings, weather sensors, zones, or the
advanced settings later, use the integration's **Configure** option — it
re-runs the same wizard, pre-filled with your current settings.

## Controls and entities

Setup creates one main **GeoDrops + Rachio Irrigation** device plus a device
per zone, with these entities (all prefixed `geodrops_rachio_`):

**Controls (main device)**

- **Buttons** — *Run irrigation now*, *Preview irrigation plan*, *Stop
  irrigation*, *Reset irrigation*, *Refresh Rachio runtimes*. Each triggers the
  scheduler's matching action; *Preview* plans without watering.
- **Drought level** (`select`) — the active drought rating (Level 0 Normal →
  Level 4 Emergency); drives target offsets, runtime scaling and rain-skip
  behavior.
- **Switches** — *Run active* (the scheduler's own run flag), *Standby* (pause
  scheduling), *Dew formed* (overnight-dew signal).
- **Status sensors** — `sensor.geodrops_rachio_status` (state is a
  human-readable status line; the raw lowercase status token, e.g. `waiting`,
  `watering`, `idle`, is in its `status` attribute — template against the
  attribute, not the state, for automations), `sensor.geodrops_rachio_last_nightly`
  (last nightly run's record), `sensor.geodrops_rachio_last_run` (last run of
  any kind, including *Run irrigation now*), and `sensor.geodrops_rachio_plan`
  (the most recent *Preview irrigation plan* output). Each carries the run's
  full detail as attributes.

**Per-zone (one device each)**

- **Exclude** (`switch`) — when on, the zone is skipped from both the nightly
  plan and calibration probing.
- **Status sensors** — soil moisture (mirrors the zone's dominant sensor),
  planned runtime, last-delivered runtime, last watered, efficacy, and
  calibration state.

**Weather (main device)**

- **Observed** and **forecast overnight** sensors for temperature, humidity and
  wind — averaged over the local 20:00→06:00 overnight window.

## Active Watering Calibration (Beta)

The **Active Watering Calibration** option in the Advanced step of the
wizard is **Beta and off by default** (`self_calibration_enabled`). Leave it
off unless you specifically want to try it; it is not required for the
scheduler's normal drought-level/soil-moisture-driven watering to work.

## Updates

Update this integration through HACS the same way you update any other
custom integration. Since the scheduler runs natively as part of the
integration's own Python, **every release requires a Home Assistant restart**
to load the updated code — HACS will flag each update this way, the same as
it would for any other custom integration.

## Upgrading from 0.9.x

Versions before v1.0.0 delivered the scheduler as a pyscript app; from
v1.0.0 on it runs natively inside this integration and pyscript is no longer
used or required.

1. Update through HACS (Download) and **restart Home Assistant**.
2. On the restart, the integration automatically deletes its old pyscript
   files and imports your calibration history from
   `/config/pyscript/geodrops_rachio_state/` — no manual steps. Once you've
   confirmed the upgrade is working, pyscript itself can be uninstalled if
   nothing else on your box uses it.
3. **Update your dashboards.** Any card or template reading
   `pyscript.geodrops_rachio_last_nightly` should switch to
   `sensor.geodrops_rachio_last_nightly` (same attribute names). Anything
   comparing `pyscript.geodrops_rachio_status`'s state against a lowercase
   token (e.g. `waiting`, `watering`) should read
   `state_attr('sensor.geodrops_rachio_status', 'status')` instead.

**Rollback:** HACS → Redownload → pick v0.9.15 → restart. Calibration learned
since the upgrade is lost (the old pyscript app doesn't see it).

## For maintainers

See [`docs/RELEASING.md`](docs/RELEASING.md) for how the scheduler brain lives
in this repo (`custom_components/geodrops_rachio/brain/`, tested in
`tests_brain/`) and how to cut a release.
