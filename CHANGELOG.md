# Changelog

Notable changes to the GeoDrops + Rachio Irrigation integration. HACS shows
each release's notes, so entries here stay user-facing and concise — one
section per released version, newest first.

## v1.0.0 — Native engine: pyscript is no longer used

### Changes

- The irrigation scheduler now runs **natively inside this integration** —
  pyscript is no longer required, no longer installed to, and no longer
  reloaded. The wizard writes straight into the config entry; there's no
  pyscript app, config YAML, or `pyscript.reload` call involved anymore.
- **Breaking: entity changes.** The old `pyscript.geodrops_rachio_*` entities
  are gone, replaced by:
  - `pyscript.geodrops_rachio_last_nightly` → `sensor.geodrops_rachio_last_nightly`
    (same attribute names).
  - `pyscript.geodrops_rachio_status` → `sensor.geodrops_rachio_status`; the
    raw lowercase status token (`waiting`, `watering`, `idle`, ...) is now in
    its `status` attribute rather than the pyscript state — read
    `state_attr('sensor.geodrops_rachio_status', 'status')` instead of
    comparing state directly.
  - `pyscript.geodrops_rachio_last_run` → `sensor.geodrops_rachio_last_run`.
  - `pyscript.geodrops_rachio_preview` → `sensor.geodrops_rachio_plan`.
  - The old calibration/targets/runtimes pyscript entities are gone; that
    data was already surfaced per-zone (efficacy, calibration state, deficit,
    refill) and stays there — it's now purely internal otherwise.
  - Update any dashboard, automation, or template that references the old
    `pyscript.*` entities before or right after upgrading.
- **Breaking: the pyscript services are gone.** `pyscript.geodrops_rachio_run_now`,
  `pyscript.geodrops_rachio_preview`, `pyscript.geodrops_rachio_stop`,
  `pyscript.geodrops_rachio_reset` and `pyscript.geodrops_rachio_refresh_runtimes`
  no longer exist. An automation or script that called one should press the
  matching button instead — `button.press` on `button.geodrops_rachio_run_now`,
  `button.geodrops_rachio_preview`, `button.geodrops_rachio_stop`,
  `button.geodrops_rachio_reset` or `button.geodrops_rachio_refresh_runtimes`.
- **Automatic migration.** On first load after upgrading, the integration
  deletes its old pyscript files, and if it removed anything and pyscript is
  loaded, reloads pyscript too (so a legacy run in progress doesn't linger).
  It then imports your calibration history from
  `/config/pyscript/geodrops_rachio_state/` into its own storage. That folder
  is left in place afterward in case you need to roll back.
- **Fix: unload no longer risks an unattended re-water.** If Home Assistant
  restarted or reloaded this integration while valves were watering, the
  scheduler used to leave Rachio's own paused-schedule auto-resume to run the
  rest of the night unsupervised. The integration now explicitly stops the
  controller and its zones on unload instead, so an interrupted night stays
  interrupted rather than resuming unattended.
- Pressing Done in the integration's options without changing anything no
  longer restarts the scheduler (so it no longer cancels a run waiting for its
  pre-dawn window); it restarts only when the configuration actually changed.
- The *Stop irrigation* button's action is now recorded in the Home
  Assistant **logbook** (via the built-in `logbook` integration, part of
  `default_config`), same as other entries the scheduler already logs.
- **Fix: a restart during the pre-dawn wait no longer loses the night.** If
  Home Assistant restarted while a planned run was waiting for its watering
  window, the startup check crashed comparing times with and without a time
  zone, so the night was silently skipped. It now re-plans that night's run
  from live moisture (or records the night as missed if the window has
  already closed), as it was always meant to.
- **Requires a Home Assistant restart** after updating — the scheduler is
  now part of the integration's own Python, so every future release will
  require a restart too (see `docs/RELEASING.md`).

**Rollback:** HACS → Redownload → v0.9.15 → restart. Calibration learned
since the upgrade is lost. v0.9.15 needs **pyscript installed and set up** — if
you uninstalled it after upgrading, reinstall it first. The new
`sensor.geodrops_rachio_last_nightly`, `sensor.geodrops_rachio_last_run` and
`sensor.geodrops_rachio_plan` entities are left behind as orphans; delete them
in Settings → Devices & services → Entities.

## v0.9.15 — Home Assistant now shows it as cloud/internet-dependent

### Changes

- Home Assistant now correctly shows this integration as internet-dependent.
  Its `iot_class` was corrected from `local_polling` to `cloud_polling` — the
  setup wizard and the scheduler reach both Rachio and GeoDrops only over the
  cloud (`api.rach.io` / BigQuery). Metadata only; scheduling and watering
  behaviour are unchanged.
- Setup docs now point to the native GeoDrops HACS integration as the simplest
  way to get soil-moisture sensors into Home Assistant, with the MQTT bridge
  kept as an off-box alternative.
- Repository housekeeping: added hassfest + HACS validation to CI.

## v0.9.14 — Faster options: edit one thing without the whole wizard

### Changes
- Opening the integration's options now lands on a single menu instead of
  re-running the full setup wizard. Change a zone, your weather sensors, or
  the advanced settings directly — Connect, Core Setup, and Weather Station
  are there when you need them, skipped when you don't.
- Every edit is saved the moment you make it, so closing the dialog partway
  through no longer loses a zone you already added or changed.
- The scheduler now restarts once, when you press Done, instead of after
  each individual change while you're still editing.
- Invalid advanced-override YAML is caught on the Advanced screen with a
  clear message, instead of failing later when the scheduler reloads.

Requires a Home Assistant restart after updating (this release changes the
integration's Python).

## v0.9.13 — New combined GeoDrops + Rachio icon

### Changes
- New integration branding. The dashboard/app icon is now a combined mark:
  the GeoDrops house and the current Rachio symbol, split by a diagonal slash.
  The logo is a matching side-by-side lockup of the two marks (no text).
  Cosmetic only — no behaviour change.

## v0.9.12 — Catch a zone whose sensor comes back overnight

### Changes
- If a zone's soil-moisture sensor is unusable at planning time (offline or
  low-quality), that zone is skipped for the night. Now, while it's still dark
  and the watering window is open, the scheduler keeps an eye on those zones: if
  the sensor recovers in time, it folds a small calibration probe for that zone
  into the same night's run instead of losing a night of calibration. Only
  affects zones that are still calibrating; nothing to configure.

Brain-only update.

## v0.9.11 — Skip zones the rain already watered

### Changes
- The nightly plan is built hours before watering starts. If rain falls in
  between, a zone's soil-moisture sensor may not have caught up by plan time — so
  the scheduler could water, or run a calibration probe on, a zone the rain had
  already soaked. It now re-reads each planned zone's live moisture right before
  watering and drops any that no longer needs it (an all-dropped night simply
  waters nothing). Less wasted water on rainy nights; nothing to configure.

Brain-only update.

## v0.9.10 — Calibration learns from the moisture peak

### Changes
- Self-calibration now learns a zone's watering response from the **peak**
  moisture rise after a probe, plus a smoothed **retention** factor for how much
  of that rise lasts — instead of a single reading hours later. A fast-draining
  zone (whose moisture spikes soon after watering, then settles) can now
  calibrate reliably rather than reading a corrupt, far-too-low response. Dosing
  still targets lasting moisture; nothing to configure.

Brain-only update (bundled scheduler v0.8.5).

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

Brain-only update (bundled scheduler v0.8.4).

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
