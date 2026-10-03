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

## What it is for

- **Water only what the soil needs.** Each night the scheduler compares every
  zone's measured soil moisture against its target and waters the deficit,
  instead of running a fixed schedule.
- **Skip or shorten watering around rain**, using the forecast and your
  weather station, and stop a running zone when it starts raining.
- **Water less during a drought** by picking a drought level: lower targets,
  shorter runtimes and an earlier finish.
- **Keep watering out of the disease window** on warm, humid, still nights.

## Supported devices

- **Controllers:** any Rachio controller the Home Assistant
  [Rachio](https://www.home-assistant.io/integrations/rachio/) integration
  supports (zones are driven through its zone switches and its
  `rachio.start_multiple_zone_schedule` / pause actions).
- **Soil moisture:** GeoDrops sensors, through
  [ha-geodrops-hacs](https://github.com/ajbaldwin/ha-geodrops-hacs) or any
  integration exposing the same dominant, state and quality sensors.
- **Weather:** any Home Assistant sensors for the five observed conditions,
  and any `weather.*` entity with an hourly forecast (see Prerequisites).

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
  with no external service to run.
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
   - **Rachio account** — your Rachio API key (Rachio web app → Account
     Settings → Get API Key). It is checked with Rachio before you continue,
     and stored in Home Assistant's own settings like any integration's key.
   - **Core bindings** — your notification service, calendar, Rachio
     controller, standby switch, and forecast entity (all picked from lists,
     not typed by hand).
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

If Rachio ever rejects the key (for example after you generate a new one),
Home Assistant shows a **Reconfigure** notice for this integration: enter the
new key there. Watering continues on each zone's stored runtimes in the
meantime; only the refresh of those runtimes from Rachio waits for the key. To
replace the key before that happens, use **⋮ → Reconfigure** on the
integration; the key must belong to the same Rachio account.

Changing an entity's ID in Home Assistant (Settings → Entities) is safe: the
integration follows renames of its own entities and of every entity you picked
in the wizard, and updates its settings to the new ID. A rename during a
watering run takes effect at once and the settings are reloaded when the run
ends. The exception is the hourly precipitation-forecast sensors, which are
found by name prefix: if you rename those, update the prefixes under
**Configure → Weather Station**.

### Configuration reference

Everything below is changed under **Configure**. Changes apply when you press
**Done**: the integration reloads, which cancels a watering run that is waiting
or in progress. Pressing **Done** with nothing changed does not reload.

| Section | What it sets |
| --- | --- |
| Connect Your Rachio Account | The API key (checked with Rachio). |
| Core Setup | Notification service, irrigation log calendar, Rachio controller, Rachio standby switch, forecast `weather.*` entity. |
| Weather station | Temperature, humidity, wind, rain-last-hour, precipitation-type and rain-today sensors; the precipitation-chance and -amount sensor-name prefixes. Rain today is the gauge's total since midnight; a night with more than `rain_confounder_mm` (0.5 mm) is left out of watering calibration. |
| Add / Edit / Remove a zone | Per zone: Rachio zone switch, GeoDrops dominant, state and quality sensors, target moisture level, full-refill runtime and depth, spray flag, grouping and adjacent zones. |
| Advanced | Active Watering Calibration on/off, and the overrides below. |

**Advanced overrides.** Any of these can be set in the overrides YAML (defaults
shown). Units are °F, mph and mm; readings in other units are converted.

| Key | Default | Meaning |
| --- | --- | --- |
| `cycle_minutes` | 12 | Longest single watering cycle before a soak. |
| `soak_minutes` | 20 | Soak between cycles of the same zone. |
| `end_offset_minutes` | 5 | Finish this many minutes before dawn/sunrise (negative: after). |
| `window_base_cap_hours` | 6.0 | Longest watering window. |
| `window_disease_cap_hours` | 3.0 | Longest window on a warm, humid, still night. |
| `warm_temp_f` | 68.0 | Overnight mean temperature that counts as warm. |
| `humid_rh_pct` | 90.0 | Overnight mean humidity that counts as humid. |
| `stagnant_wind_mph` | 2.0 | Overnight mean wind that counts as still. |
| `rain_last_hour_mm` | 0.2 | Rain gauge reading that confirms rain. |
| `rain_sustain_seconds` | 150 | How long rain must persist before a running zone stops. |
| `rain_skip_probability_pct` | 70.0 | Forecast chance of rain that can skip a zone… |
| `rain_skip_refill_fraction` | 0.5 | …when the forecast amount is at least this fraction of its refill depth. |
| `field_capacity_pct` | 87.0 | Default moisture to refill to. |
| `max_schedule_retries` | 2 | Times a schedule Rachio drops mid-run is re-issued. |

The Active Watering Calibration keys (`probe_*`, `settle_hours`,
`convergence_*` and others) are listed with comments in
[`brain/config.py`](custom_components/geodrops_rachio/brain/config.py).
`drought_profiles:` overrides per drought level (`target_offset`,
`trigger_margin`, `runtime_scale`, `rain_skip_horizon_hours`, `end_anchor`
`dawn`/`sunrise`, `end_offset_minutes`), as in the example above.

## Controls and entities

Setup creates one main **Irrigation Controls** device plus a device per
zone, with these entities (all prefixed `geodrops_rachio_`):

**Controls (main device)**

- **Buttons** — *Run irrigation now*, *Preview irrigation plan*, *Stop
  irrigation*, *Reset irrigation*, *Refresh Rachio runtimes*. Each triggers the
  scheduler's matching action; *Preview* plans without watering. *Reset* also
  discards an interrupted night, so it will not resume after a restart.
- **Drought level** (`select`) — the active drought rating (Level 0 Normal →
  Level 4 Emergency); drives target offsets, runtime scaling and rain-skip
  behavior.
- **Standby** (`switch`) — pause scheduling.
- **Run active** (`binary_sensor`) — read-only; on while the scheduler is
  running a watering schedule.
- **Status sensors** — `sensor.geodrops_rachio_status` (state is a
  human-readable status line; the raw lowercase status token, e.g. `waiting`,
  `watering`, `idle`, is in its `status` attribute — template against the
  attribute, not the state, for automations), `sensor.geodrops_rachio_last_nightly`
  (last nightly run's record), `sensor.geodrops_rachio_last_run` (last run of
  any kind, including *Run irrigation now*), and `sensor.geodrops_rachio_plan`
  (the most recent *Preview irrigation plan* output). The state of the last
  three is a zone count (zones watered, or zones the preview would water), and
  each carries the run's full detail as attributes. A `trigger` attribute says
  which kind of run it was: `nightly`, `run_now`, a startup recovery, or
  `rachio` for watering started from the Rachio app or a Rachio schedule.

**Rachio app and schedule runs** are recorded, not managed. When a zone switch
turns on while the scheduler is not watering, the run is published to *Last run*
(`trigger: rachio`) once every zone has been off for 2 minutes, with each zone's
minutes timed from its switch. It updates the zones' *last watered* and
*last-delivered runtime* sensors, and drops any calibration sample still
settling for those zones (the extra water would skew it). It does not change
planning: the moisture sensors already see the water. Timing relies on Rachio's
webhooks, so a missed one can lose or stretch a run, and a run in progress when
Home Assistant restarts is not recorded.

**Per-zone (one device each)**

- **Exclude from Watering/Calibration** (`switch`) — when on, the zone is
  skipped from both the nightly plan and calibration probing.
- **Moisture target** (`select`) — the GeoDrops band (Dry → Wet+) the
  scheduler keeps the zone at. The same setting as the zone's target moisture
  level in the options; changing it takes effect from the next plan without
  restarting the scheduler, and the zone's *Deficit* follows at once.
- **Status sensors** — soil moisture (mirrors the zone's dominant sensor),
  moisture state (mirrors the zone's GeoDrops moisture-state sensor, Dry →
  Wet+, for comparing against the moisture target), planned runtime, last-delivered runtime, last watered, efficacy, and
  calibration state. Last watered and last-delivered runtime follow the zone's
  most recent watering from any source: nightly, *Run irrigation now*, or a
  Rachio app/schedule run. Last watered is when that zone's own valve last
  closed, not when the whole run ended (for the scheduler's runs, to within
  the 30-second poll).
- Works with GeoDrops' moisture-state and quality sensors whether they report
  labels (`Moist+`, `Good`) or the translation keys newer GeoDrops versions use
  (`moist_plus`, `good`).

**Weather (main device)**

- **Forecast overnight** sensors for temperature, humidity and wind — the
  forecast averaged over the local overnight hours up to 06:00.
- **Observed overnight** sensors for the same three — what the weather station
  actually measured over last night's 23:00→06:00 (local), weighted by how long
  each reading held. They keep their readings across a restart. The 06:00
  forecast calibration compares the two. All six show in your home's units.

## How it updates

- **23:00** — the nightly run plans each zone from its current moisture and
  the overnight forecast, then waits for the watering window, which ends at
  dawn or sunrise (per drought level).
- **During a run** — zone switches are polled every 30 seconds; the rain
  sensors are watched throughout.
- **Rachio runtimes and refill depths** — fetched from the Rachio cloud when a
  plan needs them and kept for 6 hours; *Refresh Rachio runtimes* fetches now.
- **Forecast overnight sensors** — hourly, from the forecast entity.
- **Observed overnight sensors** — on every weather-station change, and every
  5 minutes.
- **06:00** — the forecast calibration compares last night's forecast with
  what was observed. Calibration samples are checked on the hour and half hour.
- **Zone sensors** (soil moisture, deficit, last watered, …) update as soon as
  their source does.

## Active Watering Calibration (Beta)

The **Active Watering Calibration** option in the Advanced step of the
wizard is **Beta and off by default** (`self_calibration_enabled`). Leave it
off unless you specifically want to try it; it is not required for the
scheduler's normal drought-level/soil-moisture-driven watering to work.

## If watering is interrupted

- **Home Assistant restarts (or crashes) mid-watering.** On startup the
  scheduler finishes that night's run: it waters **only what was still owed**,
  and only while the watering window is open (with the usual standby and rain
  checks). It never re-plans from moisture readings, which lag the watering
  by an hour or more and would water recently finished zones twice. If the
  window has already closed, the night is recorded as interrupted
  (`skipped: interrupted-restart` on `sensor.geodrops_rachio_last_run`) and
  nothing more is watered.
- **Watering is stopped outside the scheduler** — in the Rachio app, or by an
  automation. The scheduler ends that night's run and credits only the water
  actually delivered; it does not restart a schedule someone stopped. To stop
  from Home Assistant, press *Stop irrigation*.

## Examples

Tell your phone when watering starts (compare the `status` attribute, not the
display state):

```yaml
automation:
  - alias: Irrigation started
    triggers:
      - trigger: state
        entity_id: sensor.geodrops_rachio_status
        attribute: status
        to: watering
    actions:
      - action: notify.mobile_app_phone
        data:
          message: Watering started
```

Skip watering while the lawn is being mowed tomorrow, then resume:

```yaml
action: switch.turn_on
target:
  entity_id: switch.geodrops_rachio_standby
```

Keep a newly seeded zone out of the plan and calibration:
`switch.geodrops_rachio_<zone>_exclude` on.

## Known limitations

- **Rachio and GeoDrops only.** The scheduler needs Rachio zone switches and
  GeoDrops moisture sensors; there is no generic mode.
- **One instance** per Home Assistant.
- **Rachio app runs** are recorded from Rachio's webhooks: a missed webhook
  can lose or stretch one, and a run in progress across a restart is not
  recorded.
- **Precipitation-forecast sensors** are found by name prefix, so renaming
  them is not followed; update the prefixes under Configure → Weather Station.
- **Needs the Rachio cloud** for the key check and runtime refresh; watering
  continues on stored runtimes while it is unreachable.

## Troubleshooting

- **A zone was not watered.** Open `sensor.geodrops_rachio_plan` or
  `sensor.geodrops_rachio_last_nightly` in Developer Tools → States: the
  planned minutes per zone, and any zones skipped and why, are in the
  attributes. Check *Standby*, the
  zone's *Exclude* switch, and the drought level.
- **"Entities used by … are missing"** in Settings → Repairs: an entity picked
  in setup was deleted. Pick a replacement under Configure; the notice clears
  on its own.
- **Home Assistant asks for the Rachio API key**: Rachio rejected it. Enter a
  current key (Rachio web app → Account Settings → Get API Key).
- **Setup is retrying with "The advanced overrides are not valid"**: fix the
  YAML under Configure → Advanced.
- **Anything else:** download diagnostics (⋮ → Download diagnostics; the API
  key is removed) and turn on debug logging:

  ```yaml
  logger:
    logs:
      custom_components.geodrops_rachio: debug
  ```

  then open an [issue](https://github.com/ajbaldwin/ha-geodrops-rachio-irrigation/issues)
  with both.

## Removing

1. Go to **Settings → Devices & services → GeoDrops + Rachio Irrigation →
   ⋮ → Delete**. This removes its devices and entities and **deletes the
   calibration history**; Rachio's own schedules are not touched.
2. In HACS, open *GeoDrops + Rachio Irrigation* → ⋮ → **Remove**, then
   restart Home Assistant.

## Updates

Update this integration through HACS the same way you update any other
custom integration. Since the scheduler runs natively as part of the
integration's own Python, **every release requires a Home Assistant restart**
to load the updated code — HACS will flag each update this way, the same as
it would for any other custom integration.

### Beta versions

New versions are released as betas (`X.Y.Z-beta.N`) before they become a
stable release. HACS only offers betas if you opt in:

1. Go to **Settings → Devices & services → Entities**, search for
   **Pre-release**, and open the one for *GeoDrops + Rachio Irrigation* (a
   switch HACS creates for each repository, disabled by default).
2. Enable the entity, wait about 30 seconds, then turn the switch **on**.

HACS then offers each beta as an update. Turn the switch off to go back to
stable releases only; you'll get the next stable when it's newer than the beta
you're on.

## Upgrading from 0.9.x

Versions before v1.0.0 delivered the scheduler as a pyscript app; from
v1.0.0 on it runs natively inside this integration and pyscript is no longer
used or required.

1. Update through HACS (Download) and **restart Home Assistant**.
2. On the restart, the integration automatically deletes its old pyscript
   files and imports your calibration history from
   `/config/pyscript/geodrops_rachio_state/` — no manual steps. Once you've
   confirmed the upgrade is working, pyscript itself can be uninstalled if
   nothing else on your box uses it (you'd need it back to roll back — see
   below).
3. **Update your dashboards.** Any card or template reading
   `pyscript.geodrops_rachio_last_nightly` should switch to
   `sensor.geodrops_rachio_last_nightly` (same attribute names). Anything
   comparing `pyscript.geodrops_rachio_status`'s state against a lowercase
   token (e.g. `waiting`, `watering`) should read
   `state_attr('sensor.geodrops_rachio_status', 'status')` instead.
4. **Update your automations.** The `pyscript.geodrops_rachio_run_now`,
   `_preview`, `_stop`, `_reset` and `_refresh_runtimes` services are gone.
   Call `button.press` on the matching button instead:
   `button.geodrops_rachio_run_now`, `button.geodrops_rachio_preview`,
   `button.geodrops_rachio_stop`, `button.geodrops_rachio_reset` or
   `button.geodrops_rachio_refresh_runtimes`.

**Rollback:** HACS → Redownload → pick v0.9.15 → restart. Calibration learned
since the upgrade is lost (the old pyscript app doesn't see it). v0.9.15 runs
the scheduler on pyscript, so **pyscript must be installed and set up** — if
you uninstalled it after upgrading, reinstall it before the restart. After
rolling back, the v1.0.0-only `sensor.geodrops_rachio_last_nightly`,
`sensor.geodrops_rachio_last_run` and `sensor.geodrops_rachio_plan` entities
are left as orphans; delete them in Settings → Devices & services → Entities.

## For maintainers

See [`docs/RELEASING.md`](docs/RELEASING.md) for how the scheduler brain lives
in this repo (`custom_components/geodrops_rachio/brain/`, tested in
`tests_brain/`) and how to cut a release.
