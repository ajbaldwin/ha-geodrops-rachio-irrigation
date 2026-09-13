# Zone management + zones-as-devices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each irrigation zone its own HA device with an owned "exclude" switch and read-only status sensors, and add full add/edit/delete zone lifecycle to the options flow.

**Architecture:** Per-zone devices nest (`via_device`) under the single integration device. A per-zone `switch` (exclude) is integration-owned and auto-wired into the generated `config.yaml` as the zone's `exclude_boolean`. Per-zone `sensor`s read from a shared `ZoneStateCoordinator` that subscribes to the scheduler's published `last_nightly`/`preview` state entities and reads its `irrigation_efficacy.json` state file. The options flow gains a manage-zones menu (add/edit/remove); deleting a zone removes its device.

**Tech Stack:** Home Assistant custom integration (config entries, entity platforms, device/entity registry, selectors), pytest + pytest-homeassistant-custom-component, Docker test harness (`bash tools/test.sh`).

**Spec:** `docs/superpowers/specs/2026-09-13-zone-management-design.md`

## Global Constraints

- **Windows cannot run the HA test stack** (`fcntl`). Run every test with `bash tools/test.sh [path]` (Docker `geodrops-test`; start Docker Desktop first if the daemon is down).
- **No scheduler-side changes** and **no new Rachio API calls.** The integration only consumes what the vendored scheduler already publishes/persists.
- **Min HA 2026.3.0**; domain slug `geodrops_rachio`; `DOMAIN` from `const.py`.
- Every per-zone entity: `unique_id = f"{entry_id}_zone_{slug}_<suffix>"`, `device_info = zone_device_info(entry, key)`, `_attr_has_entity_name = True`, `should_poll = False`.
- `slug = _slug(key)` using the existing helper in `config_flow.py` (lowercase, non-alnum→`_`, collapse/strip `_`).
- Every status sensor degrades to `None`/`unknown` on any missing entity/attr/file/key — never raise.
- Custom-integration UI labels come from `translations/en.json` (mirror into `strings.json`); markdown in `data_description` must contain **no `<...>`** (renders as INVALID_TAG).

---

### Task 1: Auto-wire each zone's `exclude_boolean` in generated config

**Files:**
- Modify: `custom_components/geodrops_rachio/config_writer.py`
- Test: `tests/test_config_writer.py`, `tests/test_config_roundtrip.py`

**Interfaces:**
- Consumes: `_slug` — move it from `config_flow.py` to a shared spot. To avoid a circular import (`config_writer` must not import `config_flow`), **add a small `util.py`** with `slug(name)` and import it from both.
- Produces: `slug()` in `custom_components/geodrops_rachio/util.py`; `generate_config` now emits `zones.<key>.exclude_boolean = "switch.geodrops_rachio_<slug>_exclude"` unless already present.

- [ ] **Step 1: Create the shared slug helper**

Create `custom_components/geodrops_rachio/util.py`:

```python
from __future__ import annotations


def slug(name: str) -> str:
    """A config-key/object-id-safe slug (lowercase, non-alnum -> '_')."""
    out = "".join(c if c.isalnum() else "_" for c in str(name).lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")
```

Then in `config_flow.py` replace the body of the existing `_slug` with `return slug(name)` and `from .util import slug` (keep `_slug` as a thin alias so existing call sites/tests are untouched).

- [ ] **Step 2: Write the failing test**

Add to `tests/test_config_writer.py`:

```python
def test_zone_gets_owned_exclude_boolean():
    from custom_components.geodrops_rachio.config_writer import generate_config
    import yaml
    data = {
        "bindings": {},
        "zones": [{"key": "Front Slope", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": "",
    }
    raw = yaml.safe_load(generate_config(data))
    zone = raw["zones"]["Front Slope"]
    assert zone["exclude_boolean"] == "switch.geodrops_rachio_front_slope_exclude"


def test_existing_exclude_boolean_is_preserved():
    from custom_components.geodrops_rachio.config_writer import generate_config
    import yaml
    data = {"bindings": {}, "zones": [{"key": "z", "exclude_boolean": "input_boolean.custom"}],
            "self_calibration_enabled": False, "advanced_overrides": ""}
    raw = yaml.safe_load(generate_config(data))
    assert raw["zones"]["z"]["exclude_boolean"] == "input_boolean.custom"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `bash tools/test.sh tests/test_config_writer.py::test_zone_gets_owned_exclude_boolean`
Expected: FAIL (`KeyError: 'exclude_boolean'`).

- [ ] **Step 4: Implement**

In `config_writer.py`, add `from .util import slug` and change the zone-building loop:

```python
    zones = {}
    for z in data.get("zones", []):
        key = z["key"]
        zone = {k: v for k, v in z.items() if k != "key"}
        zone.setdefault(
            "exclude_boolean", f"switch.geodrops_rachio_{slug(key)}_exclude")
        zones[key] = zone
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `bash tools/test.sh tests/test_config_writer.py tests/test_config_roundtrip.py`
Expected: PASS (roundtrip still parses; `exclude_boolean` is a known ZoneConfig field).

- [ ] **Step 6: Commit**

```bash
git add custom_components/geodrops_rachio/util.py custom_components/geodrops_rachio/config_writer.py custom_components/geodrops_rachio/config_flow.py tests/test_config_writer.py
git commit -m "Auto-wire each zone's exclude_boolean to its owned switch"
```

---

### Task 2: Per-zone device info + `ZoneExcludeSwitch`

**Files:**
- Modify: `custom_components/geodrops_rachio/entity_base.py`, `custom_components/geodrops_rachio/switch.py`, `custom_components/geodrops_rachio/const.py`, `custom_components/geodrops_rachio/translations/en.json`, `custom_components/geodrops_rachio/strings.json`
- Test: `tests/test_entities.py`

**Interfaces:**
- Consumes: `slug` from `util.py`; `DOMAIN` from `const.py`.
- Produces: `entity_base.zone_device_info(entry, key) -> DeviceInfo`; `switch.ZoneExcludeSwitch`; each zone's exclude switch at `entity_id = switch.geodrops_rachio_<slug>_exclude`.

- [ ] **Step 1: Add `zone_device_info`**

Append to `entity_base.py`:

```python
from homeassistant.helpers.device_registry import DeviceInfo  # already imported
from .util import slug


def zone_device_info(entry, key: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}:zone:{slug(key)}")},
        name=str(key),
        manufacturer="GeoDrops + Rachio Irrigation",
        model="Irrigation zone",
        via_device=(DOMAIN, entry.entry_id),
    )
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_entities.py`:

```python
async def test_zone_exclude_switch_created_per_zone(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [{"key": "Front Slope", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("switch.geodrops_rachio_front_slope_exclude")
    assert state is not None
    assert state.state == "off"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `bash tools/test.sh tests/test_entities.py::test_zone_exclude_switch_created_per_zone`
Expected: FAIL (entity does not exist).

- [ ] **Step 4: Implement `ZoneExcludeSwitch` and set it up per zone**

In `switch.py` add the class and extend `async_setup_entry` (keep the existing global switches):

```python
from homeassistant.components.switch import SwitchEntity, ENTITY_ID_FORMAT
from homeassistant.helpers.restore_state import RestoreEntity
from .entity_base import zone_device_info
from .util import slug


class ZoneExcludeSwitch(SwitchEntity, RestoreEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Exclude from watering"

    def __init__(self, entry, key: str) -> None:
        s = slug(key)
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_exclude"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_exclude")
        self._attr_device_info = zone_device_info(entry, key)
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
```

In `async_setup_entry`, after adding the existing global switches:

```python
    entities.extend(
        ZoneExcludeSwitch(entry, z["key"]) for z in entry.data.get("zones", []))
```

- [ ] **Step 5: Add UI labels**

In both `translations/en.json` and `strings.json`, add under the top level an `entity` block (custom integrations read entity names from `entity.<platform>.<translation_key>` OR from `_attr_name`; `_attr_name` already covers it, so this step is only needed if a translation_key is used — since we use `_attr_name`, SKIP unless a reviewer wants localizable names). No-op unless localizing.

- [ ] **Step 6: Run tests to verify they pass**

Run: `bash tools/test.sh tests/test_entities.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add custom_components/geodrops_rachio/entity_base.py custom_components/geodrops_rachio/switch.py tests/test_entities.py
git commit -m "Per-zone device + owned exclude switch"
```

---

### Task 3: `ZoneStateCoordinator`

**Files:**
- Create: `custom_components/geodrops_rachio/coordinator.py`
- Modify: `custom_components/geodrops_rachio/const.py`, `custom_components/geodrops_rachio/__init__.py`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `coordinator.EFFICACY_STATE_FILE = "irrigation_efficacy.json"`; pure `parse_last_nightly(attrs, key) -> dict` and `parse_efficacy(store, key) -> dict`; class `ZoneStateCoordinator(hass, entry)` with `.data_for(key) -> dict`, `async def async_start()`, `async def async_refresh_file()`, and `add_listener(cb)`. Stored at `hass.data[DOMAIN][entry_id]["coordinator"]`.

- [ ] **Step 1: Write the failing test (pure parsers)**

Create `tests/test_coordinator.py`:

```python
from custom_components.geodrops_rachio.coordinator import (
    parse_last_nightly, parse_efficacy)


def test_parse_last_nightly_extracts_zone_slice():
    attrs = {
        "delivered_minutes": {"front": 42.0, "back": 10.0},
        "watered": ["front"],
        "end": "2026-09-13T06:00:00+00:00",
    }
    out = parse_last_nightly(attrs, "front")
    assert out["last_delivered_runtime"] == 42.0
    assert out["last_watered"] == "2026-09-13T06:00:00+00:00"

    # A zone that didn't water: delivered unknown, no watered timestamp.
    out_b = parse_last_nightly(attrs, "back")
    assert out_b["last_delivered_runtime"] == 10.0
    assert out_b["last_watered"] is None


def test_parse_last_nightly_missing_keys_are_none():
    assert parse_last_nightly({}, "front") == {
        "last_delivered_runtime": None, "last_watered": None}


def test_parse_efficacy_extracts_zone():
    store = {"front": {"efficacy": 0.42, "state": "converged"}}
    assert parse_efficacy(store, "front") == {
        "efficacy": 0.42, "calibration_state": "converged"}
    assert parse_efficacy(store, "missing") == {
        "efficacy": None, "calibration_state": None}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash tools/test.sh tests/test_coordinator.py`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the parsers + coordinator**

Create `coordinator.py`:

```python
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LAST_NIGHTLY_ENTITY = "pyscript.geodrops_rachio_last_nightly"
PREVIEW_ENTITY = "pyscript.geodrops_rachio_preview"
STATE_DIRNAME = "geodrops_rachio_state"
EFFICACY_STATE_FILE = "irrigation_efficacy.json"
_FILE_REFRESH = dt.timedelta(hours=1)


def parse_last_nightly(attrs: dict, key: str) -> dict:
    delivered = (attrs or {}).get("delivered_minutes") or {}
    watered = (attrs or {}).get("watered") or []
    return {
        "last_delivered_runtime": delivered.get(key),
        "last_watered": (attrs or {}).get("end") if key in watered else None,
    }


def parse_preview(attrs: dict, key: str) -> dict:
    planned = (attrs or {}).get("planned_minutes") or {}
    return {"planned_runtime": planned.get(key)}


def parse_efficacy(store: dict, key: str) -> dict:
    rec = (store or {}).get(key) or {}
    return {"efficacy": rec.get("efficacy"), "calibration_state": rec.get("state")}


class ZoneStateCoordinator:
    """Fans the scheduler's published state + efficacy file out to per-zone sensors."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._efficacy: dict = {}
        self._listeners: list = []
        self._unsubs: list = []

    def _efficacy_path(self) -> str:
        return self.hass.config.path("pyscript", STATE_DIRNAME, EFFICACY_STATE_FILE)

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    @callback
    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    def data_for(self, key: str) -> dict:
        out = {"planned_runtime": None, "last_delivered_runtime": None,
               "last_watered": None, "efficacy": None, "calibration_state": None}
        ln = self.hass.states.get(LAST_NIGHTLY_ENTITY)
        if ln is not None:
            out.update(parse_last_nightly(ln.attributes, key))
        pv = self.hass.states.get(PREVIEW_ENTITY)
        if pv is not None:
            out.update(parse_preview(pv.attributes, key))
        out.update(parse_efficacy(self._efficacy, key))
        return out

    async def async_refresh_file(self, _now=None) -> None:
        path = self._efficacy_path()

        def _read():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

        self._efficacy = await self.hass.async_add_executor_job(_read)
        self._notify()

    async def async_start(self) -> None:
        await self.async_refresh_file()

        @callback
        def _on_entity(_event):
            # last_nightly change usually means calibration ran too -> reread file.
            self.hass.async_create_task(self.async_refresh_file())

        self._unsubs.append(async_track_state_change_event(
            self.hass, [LAST_NIGHTLY_ENTITY, PREVIEW_ENTITY], _on_entity))
        self._unsubs.append(async_track_time_interval(
            self.hass, self.async_refresh_file, _FILE_REFRESH))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
```

- [ ] **Step 4: Run parser tests to verify they pass**

Run: `bash tools/test.sh tests/test_coordinator.py`
Expected: PASS.

- [ ] **Step 5: Create + start the coordinator in `__init__.py`**

In `async_setup_entry`, after storing entry data:

```python
    from .coordinator import ZoneStateCoordinator
    coordinator = ZoneStateCoordinator(hass, entry)
    await coordinator.async_start()
    hass.data[DOMAIN][entry.entry_id] = {
        "data": dict(entry.data), "coordinator": coordinator}
    entry.async_on_unload(coordinator.async_stop)
```

Update `async_unload_entry` / any existing readers of `hass.data[DOMAIN][entry_id]` to use the `"data"` sub-key. (Search the repo: `grep -rn "hass.data" custom_components/geodrops_rachio`.)

- [ ] **Step 6: Run the full suite**

Run: `bash tools/test.sh`
Expected: PASS (no reader breaks on the hass.data shape change).

- [ ] **Step 7: Commit**

```bash
git add custom_components/geodrops_rachio/coordinator.py custom_components/geodrops_rachio/__init__.py tests/test_coordinator.py
git commit -m "ZoneStateCoordinator: per-zone status from scheduler state + efficacy file"
```

---

### Task 4: Per-zone status sensors

**Files:**
- Modify: `custom_components/geodrops_rachio/sensor.py`
- Test: `tests/test_entities.py`

**Interfaces:**
- Consumes: `ZoneStateCoordinator.data_for(key)` + `add_listener`; `zone_device_info`; `slug`; the zone's `dominant_sensor` from `entry.data["zones"]`.
- Produces: six per-zone sensors per zone.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_entities.py`:

```python
async def test_zone_status_sensors(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.states.async_set("sensor.d", "71.5")  # the zone's dominant_sensor
    hass.states.async_set(
        "pyscript.geodrops_rachio_last_nightly", "1",
        {"delivered_minutes": {"front": 42.0}, "watered": ["front"],
         "end": "2026-09-13T06:00:00+00:00"})
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [{"key": "front", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.geodrops_rachio_front_soil_moisture").state == "71.5"
    assert hass.states.get("sensor.geodrops_rachio_front_last_delivered_runtime").state == "42.0"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash tools/test.sh tests/test_entities.py::test_zone_status_sensors`
Expected: FAIL (sensors don't exist).

- [ ] **Step 3: Implement**

In `sensor.py` add a coordinator-backed sensor plus a dominant-mirror sensor, and set them up per zone. Sketch (follow existing file style; `SensorDeviceClass`, `SensorEntity`, `ENTITY_ID_FORMAT`, `async_track_state_change_event` are already imported or import them):

```python
from homeassistant.components.sensor import SensorDeviceClass
from .entity_base import zone_device_info
from .util import slug

# (suffix, coordinator-key, device_class, unit)
_ZONE_FIELDS = [
    ("planned_runtime", "planned_runtime", SensorDeviceClass.DURATION, "min"),
    ("last_delivered_runtime", "last_delivered_runtime", SensorDeviceClass.DURATION, "min"),
    ("last_watered", "last_watered", SensorDeviceClass.TIMESTAMP, None),
    ("efficacy", "efficacy", None, None),
    ("calibration_state", "calibration_state", SensorDeviceClass.ENUM, None),
]


class ZoneCoordinatorSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, key, coordinator, suffix, ckey, device_class, unit):
        s = slug(key)
        self._key, self._coord, self._ckey = key, coordinator, ckey
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_{suffix}"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_{suffix}")
        self._attr_name = suffix.replace("_", " ").capitalize()
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = zone_device_info(entry, key)

    async def async_added_to_hass(self):
        self._coord.add_listener(self._update)
        self._update()

    @callback
    def _update(self):
        value = self._coord.data_for(self._key).get(self._ckey)
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP and value:
            value = dt_util.parse_datetime(value)
        self._attr_native_value = value
        if self.hass:
            self.async_write_ha_state()


class ZoneMoistureSensor(SensorEntity):
    """Live mirror of the zone's own GeoDrops dominant sensor."""
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Soil moisture"

    def __init__(self, entry, key, source):
        s = slug(key)
        self._source = source
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_soil_moisture"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_soil_moisture")
        self._attr_device_info = zone_device_info(entry, key)

    async def async_added_to_hass(self):
        @callback
        def _mirror(event=None):
            st = self.hass.states.get(self._source) if self._source else None
            self._attr_native_value = st.state if st else None
            self.async_write_ha_state()
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], _mirror))
        _mirror()
```

Add to `async_setup_entry` (needs `dt_util` import: `from homeassistant.util import dt as dt_util`):

```python
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    for z in entry.data.get("zones", []):
        entities.append(ZoneMoistureSensor(entry, z["key"], z.get("dominant_sensor")))
        for suffix, ckey, dc, unit in _ZONE_FIELDS:
            entities.append(ZoneCoordinatorSensor(entry, z["key"], coordinator, suffix, ckey, dc, unit))
```

For the ENUM sensor set `_attr_options = ["calibrating", "converged", "excluded"]` (HA requires `options` for enum device_class); if a value outside the list appears, HA logs — so instead leave `calibration_state` with **no** device_class to avoid enum validation churn. (Update `_ZONE_FIELDS` accordingly: `("calibration_state", "calibration_state", None, None)`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `bash tools/test.sh tests/test_entities.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/sensor.py tests/test_entities.py
git commit -m "Per-zone status sensors (moisture mirror + coordinator-backed)"
```

---

### Task 5: Options-flow zone lifecycle (add / edit / delete)

**Files:**
- Modify: `custom_components/geodrops_rachio/config_flow.py`, `custom_components/geodrops_rachio/translations/en.json`, `custom_components/geodrops_rachio/strings.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: existing `async_step_zone`, `async_step_zone_details`, `_append_zone`, `_build_bindings_schema`.
- Produces: `async_step_manage_zones`, `async_step_pick_zone`, `async_step_confirm_remove`; edit-aware `zone_details`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_flow.py` (reuse existing `_patch_poll`, `CONNECT_INPUT`, `BINDINGS_INPUT`, `WEATHER_INPUT`, `ZONE_INPUT`, autouse notify fixture). Build a `MockConfigEntry` with one existing zone `front`, open options, and assert:
  - the first options step is `manage_zones`;
  - choosing `remove_zone` → `pick_zone` → confirm removes `front` (entry has no zones);
  - choosing `edit_zone` → `pick_zone` (front) → `zone_details` pre-filled with the stored runtime, and submitting updates in place (still exactly one zone `front`, new runtime).

```python
async def test_options_remove_zone(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"notify_service": "notify.phone", "rachio_device_name": "Main House"},
        "zones": [dict(ZONE_INPUT)], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    with _patch_poll(key=None):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], CONNECT_INPUT)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], WEATHER_INPUT)
        assert result["step_id"] == "manage_zones"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "remove_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"zone": "front"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True})
        # back to manage_zones; finish
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"self_calibration_enabled": False})
    assert entry.data["zones"] == []
```

(Write the analogous `test_options_edit_zone_in_place` asserting one zone remains with an updated `runtime_minutes`.)

- [ ] **Step 2: Run to verify they fail**

Run: `bash tools/test.sh tests/test_config_flow.py::test_options_remove_zone`
Expected: FAIL (`manage_zones` step doesn't exist; today it's `zone_gate`).

- [ ] **Step 3: Implement the menu + steps**

In `config_flow.py`:
- In `async_step_weather`, when zones already exist (the options case) route to `async_step_manage_zones` instead of `async_step_zone_gate`.
- Add:

```python
    async def async_step_manage_zones(self, user_input=None):
        return self.async_show_menu(
            step_id="manage_zones",
            menu_options=["add_zone", "edit_zone", "remove_zone", "finish"])

    async def async_step_add_zone(self, user_input=None):
        self._editing_key = None
        return await self.async_step_zone()

    async def async_step_finish(self, user_input=None):
        return await self.async_step_advanced()

    async def async_step_edit_zone(self, user_input=None):
        self._removing = False
        return await self.async_step_pick_zone()

    async def async_step_remove_zone(self, user_input=None):
        self._removing = True
        return await self.async_step_pick_zone()

    async def async_step_pick_zone(self, user_input=None):
        keys = [z["key"] for z in self._data["zones"]]
        if user_input is not None:
            self._selected_key = user_input["zone"]
            if self._removing:
                return await self.async_step_confirm_remove()
            self._editing_key = self._selected_key
            self._picked_zone = None
            return await self.async_step_zone_details()
        schema = vol.Schema({vol.Required("zone"): selector.SelectSelector(
            selector.SelectSelectorConfig(options=keys,
                                          mode=selector.SelectSelectorMode.DROPDOWN))})
        return self.async_show_form(step_id="pick_zone", data_schema=schema)

    async def async_step_confirm_remove(self, user_input=None):
        if user_input is not None:
            if user_input.get("confirm"):
                self._data["zones"] = [
                    z for z in self._data["zones"] if z["key"] != self._selected_key]
            return await self.async_step_manage_zones()
        schema = vol.Schema({vol.Required("confirm", default=False): bool})
        return self.async_show_form(step_id="confirm_remove", data_schema=schema)
```

- Add `self._editing_key = None` and `self._selected_key = None` to both flow `__init__`s.
- Make `zone_details` edit-aware: when `self._editing_key` is set, pre-fill from the stored zone and, on submit, **replace** that zone (same key) and return to `async_step_manage_zones`; otherwise append as today. Edit pre-fill uses the stored zone dict; runtime/refill/switch defaults come from it. Keep the key immutable (don't show a key field on edit, or show it read-only).

```python
    def _stored_zone(self, key):
        return next((z for z in self._data["zones"] if z["key"] == key), {})
```

In `async_step_zone_details`'s submit branch:

```python
        if user_input is not None:
            zid = self._picked_zone["id"] if self._picked_zone else ""
            if self._editing_key:
                self._data["zones"] = [
                    z for z in self._data["zones"] if z["key"] != self._editing_key]
                self._append_zone(dict(user_input, key=self._editing_key),
                                  rachio_zone_id=zid)
                self._editing_key = None
                return await self.async_step_manage_zones()
            self._append_zone(user_input, rachio_zone_id=zid)
            if user_input.get("add_another_zone"):
                return await self.async_step_zone()
            return await self.async_step_advanced()
```

For the edit pre-fill, when building the `zone_details` schema use `self._stored_zone(self._editing_key)` as the defaults source instead of `self._picked_zone`.

- Keep `async_step_zone_gate` only if still referenced; otherwise delete it and its translations.

- [ ] **Step 4: Add UI copy**

In both JSON files, under `options.step`, add `manage_zones` (menu — needs a `menu_options` labels map under `options.step.manage_zones.menu_options`), `pick_zone`, and `confirm_remove` titles/labels. Example (`en.json`):

```json
"manage_zones": {"title": "Manage Zones",
  "menu_options": {"add_zone": "Add a zone", "edit_zone": "Edit a zone",
                   "remove_zone": "Remove a zone", "finish": "Done"}},
"pick_zone": {"title": "Choose a Zone", "data": {"zone": "Zone"}},
"confirm_remove": {"title": "Remove Zone", "data": {"confirm": "Yes, remove this zone"}}
```

Remove the now-unused `zone_gate` block if the step was deleted.

- [ ] **Step 5: Run tests to verify they pass**

Run: `bash tools/test.sh tests/test_config_flow.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/geodrops_rachio/config_flow.py custom_components/geodrops_rachio/translations/en.json custom_components/geodrops_rachio/strings.json tests/test_config_flow.py
git commit -m "Options flow: manage-zones menu (add/edit/remove)"
```

---

### Task 6: Remove orphaned zone devices on setup

**Files:**
- Modify: `custom_components/geodrops_rachio/__init__.py`
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: `slug`; `DOMAIN`.
- Produces: `_purge_orphan_zone_devices(hass, entry)` called at the end of `async_setup_entry`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_init.py`: set up an entry with zones `["front", "back"]`, then update the entry to only `["front"]` and reload; assert the device with identifier `(DOMAIN, f"{entry_id}:zone:back")` is gone from the device registry while `:zone:front` remains.

```python
async def test_removing_zone_purges_its_device(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from homeassistant.helpers import device_registry as dr
    from custom_components.geodrops_rachio.const import DOMAIN
    def _zone(k):
        return {"key": k, "rachio_switch": "switch.x", "dominant_sensor": "sensor.d",
                "state_sensor": "sensor.s", "quality_sensors": [], "target_range": "moist",
                "runtime_minutes": 20, "refill_depth_mm": 10}
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [_zone("front"), _zone("back")],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = dr.async_get(hass)
    assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:back")})

    hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": [_zone("front")]})
    await hass.async_block_till_done()  # triggers reload via existing options listener
    assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:back")}) is None
    assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:front")})
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash tools/test.sh tests/test_init.py::test_removing_zone_purges_its_device`
Expected: FAIL (orphan `back` device still present).

- [ ] **Step 3: Implement**

In `__init__.py`:

```python
from homeassistant.helpers import device_registry as dr
from .util import slug


def _purge_orphan_zone_devices(hass, entry) -> None:
    reg = dr.async_get(hass)
    keep = {f"{entry.entry_id}:zone:{slug(z['key'])}"
            for z in entry.data.get("zones", [])}
    for device in dr.async_entries_for_config_entry(reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain == DOMAIN and ":zone:" in ident and ident not in keep:
                reg.async_remove_device(device.id)
                break
```

Call `_purge_orphan_zone_devices(hass, entry)` at the end of `async_setup_entry` (after `async_forward_entry_setups`, so kept devices exist before pruning).

- [ ] **Step 4: Run tests to verify they pass**

Run: `bash tools/test.sh tests/test_init.py`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
bash tools/test.sh
git add custom_components/geodrops_rachio/__init__.py tests/test_init.py
git commit -m "Purge orphaned per-zone devices on setup"
```

---

## Self-review notes

- **Spec coverage:** device (T2), exclude switch + config wiring (T1/T2), status sensors + data sources (T3/T4), add/edit/delete lifecycle (T5), delete cleanup (T6), back-compat (T1 `setdefault`, T2/T4 create-on-reload). `deficit` intentionally dropped (spec Out-of-scope).
- **Interfaces:** `slug` (util.py, T1) used by T2/T4/T6; `ZoneStateCoordinator.data_for` (T3) used by T4; hass.data `"coordinator"` key (T3) read by T4.
- **Risk R1 (attribute keys):** pinned in T3 tests (`delivered_minutes`, `watered`, `end`, `planned_minutes`); sensors degrade to `None` otherwise.
- **Verify before "done":** on hardware, add a second zone, confirm its device + entities appear, toggle exclude and confirm the generated `config.yaml` binds it + the scheduler skips the zone; delete it and confirm its device disappears.
