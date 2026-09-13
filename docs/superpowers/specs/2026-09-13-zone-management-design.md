# Zone management + zones-as-devices — design

**Date:** 2026-09-13
**Status:** approved design, pre-plan
**Repo:** `ajbaldwin/ha-geodrops-rachio-irrigation` (the HA integration wrapper)
**Depends on:** the persisted-entity namespacing fix (PR #4, `fix/namespace-persisted-entities`)
must be merged to `main` first — the per-zone status sensors read
`pyscript.geodrops_rachio_last_nightly`, which that PR finishes namespacing.

## Problem

Today the integration is a single HA **device** that owns integration-wide
entities (drought select, stop button, run-active/standby/dew switches, overnight
weather sensors). Zones are **config-entry data** consumed by the vendored
pyscript scheduler; they have no presence in HA. Two gaps:

1. **No per-zone visibility or control.** A user can't see a zone's moisture,
   what it watered, or its calibration state, and can't exclude a single zone,
   without hand-editing helpers.
2. **Zones are add-only.** The options flow can only append zones
   (`async_step_zone_gate` → add). There is no way to **edit** or **delete** an
   existing zone from the UI.

## Goals

- Each configured zone becomes its own HA **device**, grouping integration-owned
  per-zone entities.
- Per-zone **status** (read-only) and one per-zone **control** (exclude).
- Full zone **lifecycle** in the options flow: add, edit, delete.

## Non-goals (explicit scope guard)

- **No scheduler-side changes.** The integration only *consumes* what the
  vendored scheduler already publishes/persists. (Calibration/efficacy is read
  from the scheduler's state file — see Data sources.)
- **No new Rachio API calls** beyond the wizard-time poll already shipped.
- The zone's **switch** stays the single owned *control*; the zone's moisture
  sensors (GeoDrops) and watering switch (Rachio) remain owned by their own
  integrations — we do not re-parent them.
- No historical/statistics store beyond what HA records for our entities.

## Design

### 1. Per-zone device

For each zone in `entry.data["zones"]`, register a device:

- `identifiers = {(DOMAIN, f"{entry_id}:zone:{slug}")}` where `slug` is the
  zone `key` normalized to a safe object-id (`_slug`, already in `config_flow`).
- `via_device = (DOMAIN, entry_id)` — nests each zone device under the main
  integration device.
- `name` = the zone's friendly name (fall back to the key).
- `manufacturer`/`model` set to something informative (e.g. model "Irrigation
  zone").

A shared helper `entity_base.zone_device_info(entry, key)` returns this
`DeviceInfo`, mirroring the existing `device_info(entry)`.

### 2. Owned entities per zone

All carry `unique_id = f"{entry_id}_zone_{slug}_<suffix>"`,
`device_info = zone_device_info(...)`, `_attr_has_entity_name = True`, and
`should_poll = False`.

**Control (new `switch` entities, in addition to the existing global switches):**

| suffix | class | behavior |
|--------|-------|----------|
| `exclude` | `SwitchEntity` + `RestoreEntity` | Integration-owned. Default **off** (zone included). Persists across restarts. When **on**, the scheduler drops this zone from the nightly plan and calibration probing. |

**Status (new `sensor` entities):**

| suffix | source | device_class / unit | notes |
|--------|--------|---------------------|-------|
| `soil_moisture` | the zone's own `dominant_sensor` (from zone config) | none / `pts` (0–100) | live mirror of the GeoDrops dominant sensor, attached to the zone device |
| `planned_runtime` | `pyscript.geodrops_rachio_preview` attr `planned_minutes[key]` | duration / `min` | minutes the last preview/plan would water |
| `last_delivered_runtime` | `pyscript.geodrops_rachio_last_nightly` attr `delivered_minutes[key]` | duration / `min` | minutes actually delivered last nightly |
| `last_watered` | `last_nightly` attr `end` when `key in last_nightly.watered` | `timestamp` | last night the zone watered |
| `calibration_state` | efficacy file `[key]["state"]` | enum | `calibrating` / `converged` / … |
| `efficacy` | efficacy file `[key]["efficacy"]` | none / `pts/min` | null while calibrating |

Every sensor **degrades to `unknown`** when its source key/attribute/file/entity
is absent, so a partial payload or a fresh install never raises.

**Dropped from v1:** a per-zone `deficit` sensor — deficit = target − dominant,
but `target` is computed inside the scheduler from the drought level + bands and
is not published; deriving it in the integration would duplicate scheduler logic.
Deferred until the scheduler publishes it (see Out of scope).

### 3. Data sources (read-only)

Sources, all already produced by the scheduler; no scheduler change. A single
**coordinator** owns them and fans values to the per-zone entities:

1. **Published entities** — subscribe via `async_track_state_change_event`:
   - `pyscript.geodrops_rachio_last_nightly` (namespaced by PR #4). Confirmed
     attribute shape: `delivered_minutes` (`{key: minutes}`), `watered`
     (`[key, …]`), `end` (ISO timestamp), and `calibration`
     (`{key: {state, efficacy, …}}` — for watered zones only). Sources
     `last_delivered_runtime` and `last_watered`.
   - `pyscript.geodrops_rachio_preview` — attribute `planned_minutes`
     (`{key: minutes}`). Sources `planned_runtime`. May be empty on a fresh box
     → `unknown`.
   - The zone's own `dominant_sensor` (an entity id from the zone config) →
     `soil_moisture` mirror.

2. **State file** `<config>/pyscript/geodrops_rachio_state/irrigation_efficacy.json`
   (confirmed name — only the *directory* is namespaced; `EFFICACY_PATH` in the
   scheduler is `STATE_DIR + "/irrigation_efficacy.json"`), shape
   `{zone_key: {"efficacy", "span_pts", "state", "n_obs", …}}`. Read via
   `hass.async_add_executor_job` (file I/O off the loop). Covers **all** zones
   (the `last_nightly.calibration` attr covers only zones that watered), so it is
   the source for `calibration_state` + `efficacy`. Refreshed on startup, on each
   `last_nightly` change, and on a slow periodic fallback (hourly). The path is a
   `const.py` constant shared with `delivery`.

A small **`ZoneStateCoordinator`** (plain object, not necessarily a
`DataUpdateCoordinator`) owns both sources and exposes
`data_for(key) -> dict`. Per-zone sensors read from it and subscribe to its
updates. This keeps parsing in one place and out of each entity.

### 4. Exclude wiring (config generation)

The `ZoneExcludeSwitch` sets its `entity_id` **explicitly** to
`switch.geodrops_rachio_<slug>_exclude` (as the existing global switches set
theirs), so the id is deterministic from the key rather than derived from the
name. `config_writer.generate_config` sets each zone's
`exclude_boolean` to that id automatically (unless the stored zone already
specifies one). The vendored scheduler already reads `exclude_boolean`
(`config.py`: `exclude_boolean=z.get("exclude_boolean", "")`), so no scheduler
change is needed. Existing entries gain `exclude_boolean` on their next config
regeneration (default switch state off = included, so behavior is unchanged).

### 5. Zone lifecycle (options flow)

Replace the add-only `async_step_zone_gate` with a **manage-zones menu**:

- `async_step_manage_zones` — `async_show_menu` with options:
  - **add_zone** → the existing add path (`async_step_zone` picker →
    `async_step_zone_details`).
  - **edit_zone** → `async_step_pick_zone` (a `SelectSelector` of existing zone
    keys) → `async_step_zone_details` **pre-filled from the stored zone** and
    writing back in place (matched by key) instead of appending.
  - **remove_zone** → `async_step_pick_zone` (select) → `async_step_confirm_remove`
    (a boolean confirm) → drop the zone from `self._data["zones"]`.
  - **finish** → `async_step_advanced` → persist.

`zone_details` is parameterized to know whether it is **adding** (append) or
**editing** (replace the zone with the same key). Editing keeps the key
immutable (the key anchors the device/entity ids); changing a zone's identity =
delete + add.

The config-flow (first install) wizard is unchanged except that the manage-zones
menu is the options-flow entry point in place of `zone_gate`.

### 6. Delete / cleanup semantics

Deleting a zone must not leave orphan devices/entities:

- On `async_setup_entry` (which runs after every options save via the existing
  reload), reconcile the device registry: for every geodrops_rachio zone device
  whose `slug` is **not** in the current `entry.data["zones"]`,
  `device_registry.async_remove_device(...)` (this cascades its entities).
- Re-delivery already regenerates `config.yaml` without the removed zone and
  `pyscript.reload`s (existing delivery path), so the scheduler stops planning
  it.

### 7. Back-compat / migration

- Entries created before this feature have zones with no owned exclude switch
  and no per-zone devices. On upgrade + reload: per-zone devices/entities are
  created, and `config_writer` adds `exclude_boolean`. No stored-data migration
  is required; `switch` default off preserves prior behavior.
- Zone `key` values are user free-text; entity/device ids use `_slug(key)`.
  Two keys that slug to the same id would collide — validated in the zone step
  (reject a key whose slug duplicates an existing zone's slug).

## Components / files

- `entity_base.py` — add `zone_device_info(entry, key)`.
- `const.py` — add the efficacy-file name / state-dir constant (shared with
  `delivery`), and the per-zone sensor descriptors.
- `coordinator.py` (**new**) — `ZoneStateCoordinator`: subscribes to
  `last_nightly`, reads the efficacy file, exposes `data_for(key)` + update
  signal.
- `switch.py` — add per-zone `ZoneExcludeSwitch` set up per zone (alongside the
  existing global switches).
- `sensor.py` — add the seven per-zone sensor classes / one descriptor-driven
  class, set up per zone.
- `config_writer.py` — inject `exclude_boolean` per zone.
- `config_flow.py` — replace `zone_gate` with the manage-zones menu; add
  `pick_zone`, `confirm_remove`; parameterize `zone_details` for edit vs add.
- `__init__.py` — device-registry reconciliation (remove orphaned zone devices)
  on setup.
- `strings.json` + `translations/en.json` — menu, pick/confirm steps, per-zone
  entity names.
- Tests — see below.

## Testing (TDD)

- **config_writer**: each zone emits `exclude_boolean = switch.geodrops_rachio_<slug>_exclude`;
  a zone that already had one keeps it; round-trip still parses in the vendored
  scheduler.
- **switch**: per-zone exclude switch is created per configured zone; default
  off; restores its state; `unique_id`/device wiring correct.
- **sensor / coordinator**: given a synthetic `last_nightly` state + a synthetic
  efficacy file, each sensor reports the right value; missing key/attr/file →
  `unknown`; values update when `last_nightly` changes.
- **options flow**: manage-zones menu routes to add / edit / remove; **edit**
  updates the zone in place (same key) and pre-fills; **remove** drops it;
  **finish** persists.
- **cleanup**: after removing a zone and reloading, that zone's device is gone
  from the registry; remaining zones' devices persist.
- All via `bash tools/test.sh` (Docker; Windows can't run the HA stack).

## Risks / open questions

- **R1 — `last_nightly` attribute shape.** The exact per-zone keys
  (`delivered`, `deficit_pts`, `watered`, timestamp field) must be confirmed
  against the vendored script during implementation. Mitigation: sensors degrade
  to `unknown` on any missing key, and a coordinator unit test pins the shape we
  rely on.
- **R2 — file-format coupling.** Reading `geodrops_rachio_efficacy.json` couples
  us to the scheduler's internal file shape. Accepted (chosen over a scheduler
  change); isolated in the coordinator so a future move to a published state
  entity is a one-file change.
- **R3 — key/slug collisions.** Guarded by validation in the zone step.
- **R4 — entity churn on rename.** Editing keeps the key immutable to avoid
  device/entity id churn; identity changes are delete + add.

## Out of scope / future

- Publishing per-zone calibration/efficacy as a proper scheduler state entity
  (removes R2) — a later scheduler PR.
- Per-zone "water now for N minutes" control (a new owned button/number) — a
  possible follow-up once the read-only surface is proven.
