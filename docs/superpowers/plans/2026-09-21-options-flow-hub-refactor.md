# Options Flow Hub Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the GeoDrops + Rachio options flow into a flat hub menu whose every sub-step persists to the config entry immediately, with the scheduler reload deferred to a single restart on "Done".

**Architecture:** The options flow's `async_step_init` seeds `_data` fully from the existing entry, sets a per-entry `suppress_reload` flag, auto-connects to Rachio, then shows a new `async_step_menu` hub. Each options sub-step (connect, bindings, weather, add/edit/remove zone, advanced) writes `_data` to the entry via a `_persist()` helper and returns to the hub. The `_reload_on_options` update listener no-ops while `suppress_reload` is set, so the mid-flow `async_update_entry` calls never restart the scheduler; "Done" clears the flag and fires exactly one `async_reload`. The first-install config flow keeps its linear wizard unchanged.

**Tech Stack:** Python 3.13, Home Assistant custom component, voluptuous, pytest + pytest-homeassistant-custom-component. Tests run in Docker (native Windows lacks `fcntl`).

**Spec:** `docs/superpowers/specs/2026-09-21-options-flow-hub-refactor-design.md`

## Global Constraints

- **First-install (config) flow behavior is unchanged.** No seeding, no `_persist()`, no reload guard in `GeodropsRachioConfigFlow`. All 14 existing config-flow tests must stay green.
- **No changes to field sets, validation semantics, Rachio polling, or the generated `config.yaml` schema.** After any sub-step, `_data["bindings"]` must be a complete, valid `HABindings` dict (produced by `_assemble_bindings`, identical to config-flow output).
- **`strings.json` and `translations/en.json` are byte-identical** and must be edited together (verify with `diff`).
- **Test runner** (Git Bash must disable MSYS path conversion):
  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest <target> -q
  ```
  The `ghr-test` image is python:3.13 + `requirements_test.txt`. Rebuild it from a Dockerfile (`COPY requirements_test.txt` then `pip install -r requirements_test.txt`) if missing.
- **Commit attribution:** end every commit message with
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

## File Structure

| File | Responsibility after this plan |
|------|-------------------------------|
| `custom_components/geodrops_rachio/__init__.py` | `_reload_on_options` honors the per-entry `suppress_reload` flag. |
| `custom_components/geodrops_rachio/config_flow.py` | Shared wizard steps + `GeodropsRachioOptionsFlow` hub: seed, suppress, auto-connect, `async_step_menu`, per-sub-step `_persist()`, advanced YAML validation, `finish` = save + single reload. |
| `custom_components/geodrops_rachio/strings.json` | Rename `options.step.manage_zones` → `options.step.menu` (hub title + 8 menu items); add `options.error.invalid_advanced_overrides`. |
| `custom_components/geodrops_rachio/translations/en.json` | Identical mirror of `strings.json`. |
| `tests/test_init.py` | Reload-suppression listener test. |
| `tests/test_config_flow.py` | Rewritten + new options-hub tests (nav, seed, persist, edit-independence, connect, reload count). |

---

### Task 1: Reload suppression flag in `__init__.py`

Make the options-reload update listener a no-op while an options flow is active, driven by a `suppress_reload` flag stored in `hass.data[DOMAIN][entry_id]`.

**Files:**
- Modify: `custom_components/geodrops_rachio/__init__.py:46-47`
- Test: `tests/test_init.py`

**Interfaces:**
- Produces: the contract that `_reload_on_options(hass, entry)` skips `async_reload` when `hass.data[DOMAIN][entry_id]["suppress_reload"]` is truthy. Task 2's options flow sets/clears this flag.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_init.py` (it already imports `AsyncMock, patch`, `ConfigEntryState`, `MockConfigEntry`, `DOMAIN`):

```python
async def test_reload_listener_honors_suppress_flag(hass, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.delivery.async_deliver",
               AsyncMock(return_value=True)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reloads = []

        async def _fake_reload(entry_id):
            reloads.append(entry_id)

        with patch.object(hass.config_entries, "async_reload", _fake_reload):
            # Flag set -> update fires the listener but it must NOT reload.
            hass.data[DOMAIN][entry.entry_id]["suppress_reload"] = True
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "advanced_overrides": "a: 1"})
            await hass.async_block_till_done()
            assert reloads == []

            # Flag cleared -> the next data change reloads exactly once.
            hass.data[DOMAIN][entry.entry_id]["suppress_reload"] = False
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "advanced_overrides": "a: 2"})
            await hass.async_block_till_done()
            assert reloads == [entry.entry_id]
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest tests/test_init.py::test_reload_listener_honors_suppress_flag -q
```
Expected: FAIL — `assert reloads == []` fails because the current listener always reloads (first update produces `reloads == [entry_id]`).

- [ ] **Step 3: Write minimal implementation**

Replace `custom_components/geodrops_rachio/__init__.py` lines 46-47:

```python
async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # An options flow persists each edit via async_update_entry, which fires
    # this listener. While the dialog is open we defer the scheduler restart:
    # the options flow sets suppress_reload and, on "Done", clears it and fires
    # exactly one reload. See config_flow.GeodropsRachioOptionsFlow.
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if store.get("suppress_reload"):
        return
    await hass.config_entries.async_reload(entry.entry_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest tests/test_init.py -q
```
Expected: PASS (new test + the 3 existing init tests, including `test_removing_zone_purges_its_device`, which has no flag set so the listener still reloads).

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/__init__.py tests/test_init.py
git commit -m "feat(options): honor suppress_reload flag in reload listener

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Options flow becomes a hub

Convert `GeodropsRachioOptionsFlow` to land on a flat hub menu. Seed `_data` from the entry, set `suppress_reload`, auto-connect, and route every sub-step through `_persist()` back to the hub. `finish` clears the flag, reloads once, and exits. This is one atomic change: partial application leaves the options tests red, so implementation and its tests land together.

**Files:**
- Modify: `custom_components/geodrops_rachio/config_flow.py` (many methods — full replacements below)
- Modify: `custom_components/geodrops_rachio/strings.json`
- Modify: `custom_components/geodrops_rachio/translations/en.json`
- Test: `tests/test_config_flow.py` (rewrite the 7 options tests, add new hub tests), `tests/test_init.py` (add reload-count-through-flow test)

**Interfaces:**
- Consumes: the `suppress_reload` contract from Task 1.
- Produces:
  - `async_step_menu(self, user_input=None)` — the options hub; `step_id="menu"`; menu items `connect, bindings, weather, add_zone, edit_zone, remove_zone, advanced, finish`.
  - `_persist(self) -> None` — `self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)`.
  - `_core_from_bindings(self) -> dict` / `_weather_form_from_bindings(self) -> dict` — reconstruct a step's input dict from `_data["bindings"]` so Core and Weather edit independently.
  - Options `_async_finish` clears `suppress_reload`, calls `async_reload` once, returns `async_create_entry(title="", data={})`.
  - `async_step_manage_zones` is **removed**.

- [ ] **Step 1: Rewrite the options tests to the new hub behavior**

In `tests/test_config_flow.py`, add a reload-count helper near the top (after `_patch_poll`):

```python
@contextlib.contextmanager
def _count_reloads(hass):
    """Patch async_reload to a counting no-op so options tests can run without
    setting up the entry and can assert the single 'Done' reload."""
    calls = []

    async def _fake_reload(entry_id):
        calls.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        yield calls
```

Replace **all seven** existing options tests (`test_options_flow_prefills_and_preserves_zones`, `test_options_remove_zone`, `test_options_edit_zone_in_place`, `test_options_add_zone_duplicate_key_rejected`, `test_options_edit_zone_preserves_rachio_zone_id`, `test_options_add_zone_returns_to_menu_and_persists`, `test_options_add_two_zones_via_menu`) and the `_zone` helper with the block below. Keep the module-level constants (`CONNECT_INPUT`, `BINDINGS_INPUT`, `WEATHER_INPUT`, `ZONE_INPUT`, `DEVICES`, `LIVE_ZONES`) and the `ORIGINAL_BINDINGS` fixture data.

```python
ORIGINAL_BINDINGS = {
    "notify_service": "notify.phone",
    "calendar_entity": "calendar.lawn",
    "rachio_device_name": "Main House",
    "rachio_api_key_secret": "rachio_api_key",
    "standby_switch": "switch.sprinkler_standby",
    "forecast_entity": "weather.home",
    "weather": {
        "temperature": "sensor.tempest_sensor_temperature",
        "humidity": "sensor.tempest_sensor_humidity",
        "wind": "sensor.tempest_sensor_wind_speed_average",
        "rain_last_hour": "sensor.tempest_rain_last_hour",
        "precip_type": "sensor.tempest_sensor_precipitation_type",
    },
    "derived": {
        "precipitation_chance_prefix": "sensor.precipitation_chance_",
        "precipitation_amount_prefix": "sensor.precipitation_amount_",
        "forecast_overnight": {
            "temp": "sensor.geodrops_rachio_forecast_overnight_temp",
            "humidity": "sensor.geodrops_rachio_forecast_overnight_humidity",
            "wind": "sensor.geodrops_rachio_forecast_overnight_wind",
        },
        "observed_overnight": {
            "temp": "sensor.geodrops_rachio_observed_overnight_temp",
            "humidity": "sensor.geodrops_rachio_observed_overnight_humidity",
            "wind": "sensor.geodrops_rachio_observed_overnight_wind",
        },
    },
    "drought_level_select": "select.geodrops_rachio_drought_level",
    "standby_boolean": "switch.geodrops_rachio_standby",
    "dew_formed_boolean": "switch.geodrops_rachio_dew_formed",
    "run_active_boolean": "switch.geodrops_rachio_run_active",
    "sun": {"dawn": "sensor.sun_next_dawn", "sunrise": "sensor.sun_next_rising"},
}


def _options_entry(hass, *, zones=None, bindings=None, calib=False, overrides=""):
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": bindings if bindings is not None else dict(ORIGINAL_BINDINGS),
        "zones": zones if zones is not None else [dict(ZONE_INPUT)],
        "self_calibration_enabled": calib,
        "advanced_overrides": overrides})
    entry.add_to_hass(hass)
    return entry


def _zone(key, switch):
    # The options add form hides add_another_zone, so don't submit it there.
    d = dict(ZONE_INPUT, key=key, rachio_switch=switch)
    d.pop("add_another_zone", None)
    return d


async def test_options_lands_on_hub_menu(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "menu"
    assert set(result["menu_options"]) == {
        "connect", "bindings", "weather", "add_zone", "edit_zone",
        "remove_zone", "advanced", "finish"}


async def test_options_seed_finish_leaves_entry_unchanged(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass, zones=[dict(ZONE_INPUT, add_another_zone=None)],
                           calib=False, overrides="")
    original = dict(entry.data)
    with _patch_poll(key=None), _count_reloads(hass) as reloads:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert reloads == []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert reloads == [entry.entry_id]          # exactly one reload, at finish
    assert entry.data["bindings"] == original["bindings"]
    assert entry.data["zones"] == original["zones"]
    assert entry.data["self_calibration_enabled"] is False
    assert entry.data["advanced_overrides"] == ""


async def test_options_add_zone_persists_before_finish(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass) as reloads:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_zone"})
        assert result["step_id"] == "zone"
        assert "add_another_zone" not in {str(f) for f in result["data_schema"].schema}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _zone("back", "switch.back"))
        assert result["step_id"] == "menu"
        # Persisted to the entry immediately, before Done, with no reload yet.
        assert [z["key"] for z in entry.data["zones"]] == ["front", "back"]
        assert reloads == []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert reloads == [entry.entry_id]


async def test_options_add_zone_abandoned_still_persisted(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass) as reloads:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _zone("back", "switch.back"))
        assert result["step_id"] == "menu"
    # Dialog abandoned (no finish): edit is durable, no reload fired.
    assert [z["key"] for z in entry.data["zones"]] == ["front", "back"]
    assert reloads == []


async def test_options_add_two_zones_via_menu(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        for key, sw in (("back", "switch.back"), ("side", "switch.side")):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "add_zone"})
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _zone(key, sw))
            assert result["step_id"] == "menu"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert [z["key"] for z in entry.data["zones"]] == ["front", "back", "side"]


async def test_options_remove_zone(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "remove_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"zone": "front"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True})
        assert result["step_id"] == "menu"
        assert entry.data["zones"] == []           # persisted on removal
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
    assert entry.data["zones"] == []


async def test_options_edit_zone_in_place(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "edit_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"zone": "front"})
        assert result["step_id"] == "zone_details"
        assert "key" not in _field_names(result)
        defaults = {str(f): f.default() for f in result["data_schema"].schema
                    if callable(getattr(f, "default", None)) and f.default() is not None}
        assert defaults.get("runtime_minutes") == 45
        assert defaults.get("rachio_switch") == "switch.front"

        updated = dict(ZONE_INPUT)
        del updated["key"]
        del updated["add_another_zone"]
        updated["runtime_minutes"] = 60
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], updated)
        assert result["step_id"] == "menu"
        assert entry.data["zones"][0]["runtime_minutes"] == 60   # persisted on edit
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
    assert len(entry.data["zones"]) == 1
    assert entry.data["zones"][0]["runtime_minutes"] == 60


async def test_options_edit_zone_preserves_rachio_zone_id(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass, zones=[dict(ZONE_INPUT, rachio_zone_id="z-uuid-1")])
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "edit_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"zone": "front"})
        updated = dict(ZONE_INPUT)
        del updated["key"]
        del updated["add_another_zone"]
        updated["runtime_minutes"] = 60
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], updated)
        assert result["step_id"] == "menu"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
    assert entry.data["zones"][0]["rachio_zone_id"] == "z-uuid-1"
    assert entry.data["zones"][0]["runtime_minutes"] == 60


async def test_options_add_zone_duplicate_key_rejected(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_zone"})
        assert result["step_id"] == "zone"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _zone("Front", "switch.front2"))
        assert result["step_id"] == "zone"
        assert result["errors"] == {"key": "duplicate_zone_key"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _zone("back", "switch.back"))
        assert result["step_id"] == "menu"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
    assert sorted(z["key"] for z in entry.data["zones"]) == ["back", "front"]


async def test_options_bindings_prefills_and_edits_core_only(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "bindings"})
        assert result["step_id"] == "bindings"
        notify_field = next(
            f for f in result["data_schema"].schema if f == "notify_service")
        assert notify_field.default() == "notify.phone"
        edited = dict(BINDINGS_INPUT, calendar_entity="calendar.changed")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], edited)
        assert result["step_id"] == "menu"
    # Core changed; weather sub-dict preserved verbatim (edit independence).
    assert entry.data["bindings"]["calendar_entity"] == "calendar.changed"
    assert entry.data["bindings"]["weather"] == ORIGINAL_BINDINGS["weather"]
    assert entry.data["bindings"]["derived"]["precipitation_chance_prefix"] == (
        "sensor.precipitation_chance_")


async def test_options_weather_edit_preserves_core(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    changed_weather = dict(WEATHER_INPUT, weather_humidity="sensor.new_humidity")
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "weather"})
        assert result["step_id"] == "weather"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], changed_weather)
        assert result["step_id"] == "menu"
    assert entry.data["bindings"]["weather"]["humidity"] == "sensor.new_humidity"
    # Core preserved from the original entry.
    assert entry.data["bindings"]["calendar_entity"] == "calendar.lawn"
    assert entry.data["bindings"]["notify_service"] == "notify.phone"


async def test_options_connect_repoll_persists_secret(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "connect"})
        assert result["step_id"] == "connect"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"rachio_api_key_secret": "rotated_key"})
        assert result["step_id"] == "menu"
    assert entry.data["bindings"]["rachio_api_key_secret"] == "rotated_key"
```

- [ ] **Step 2: Run the options tests to verify they fail**

Run:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest tests/test_config_flow.py -q -k options
```
Expected: FAIL — the current options flow opens on `connect`, not `menu`, and has no `add_zone` menu item on init, so `test_options_lands_on_hub_menu` and the rest fail.

- [ ] **Step 3: Add shared helpers and the import to `config_flow.py`**

At the top of `config_flow.py`, after the existing `from .rachio_client import (...)` block, add:

```python
from .config_writer import generate_config
```

Inside `_BindingsWizardSteps`, add these three methods (place them just after `_existing_bindings`, around line 166):

```python
    def _persist(self) -> None:
        """Options-only: write the in-progress _data to the config entry now.

        The reload listener is suppressed while the dialog is open (see
        __init__._reload_on_options), so this saves each edit durably WITHOUT
        restarting the scheduler mid-flow. The single reload happens on "Done".
        """
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self._data)

    def _core_from_bindings(self) -> dict[str, Any]:
        """The Core-step field set, read back out of _data['bindings'].

        Lets the Weather sub-step re-assemble a complete bindings dict from the
        existing core without the admin re-walking Core.
        """
        b = self._data.get("bindings", {})
        return {
            "notify_service": b.get("notify_service"),
            "calendar_entity": b.get("calendar_entity"),
            "rachio_device_name": b.get("rachio_device_name"),
            "rachio_api_key_secret": b.get("rachio_api_key_secret", self._secret_name),
            "standby_switch": b.get("standby_switch"),
            "forecast_entity": b.get("forecast_entity"),
        }

    def _weather_form_from_bindings(self) -> dict[str, Any]:
        """The Weather-step form dict, reconstructed from _data['bindings'], so
        the Core sub-step can re-assemble a complete bindings dict without the
        admin re-walking Weather."""
        b = self._data.get("bindings", {})
        w = b.get("weather", {})
        d = b.get("derived", {})
        return {
            "weather_temperature": w.get("temperature", DEFAULT_WEATHER["temperature"]),
            "weather_humidity": w.get("humidity", DEFAULT_WEATHER["humidity"]),
            "weather_wind": w.get("wind", DEFAULT_WEATHER["wind"]),
            "weather_rain_last_hour": w.get(
                "rain_last_hour", DEFAULT_WEATHER["rain_last_hour"]),
            "weather_precip_type": w.get(
                "precip_type", DEFAULT_WEATHER["precip_type"]),
            "precipitation_chance_prefix": d.get(
                "precipitation_chance_prefix", DEFAULT_PRECIPITATION_CHANCE_PREFIX),
            "precipitation_amount_prefix": d.get(
                "precipitation_amount_prefix", DEFAULT_PRECIPITATION_AMOUNT_PREFIX),
        }
```

- [ ] **Step 4: Replace `async_step_manage_zones` with `async_step_menu`**

Replace the whole `async_step_manage_zones` method (lines 376-392) with:

```python
    async def async_step_menu(self, user_input=None):
        """Options hub. Every sub-step returns here; 'Done' saves and reloads.

        Inline label dict (not a translated list) so the menu never renders
        blank before this integration's translations load.
        """
        return self.async_show_menu(
            step_id="menu",
            menu_options={
                "connect": "Connect Your Rachio Account",
                "bindings": "Core Setup",
                "weather": "Weather Station",
                "add_zone": "Add a zone",
                "edit_zone": "Edit a zone",
                "remove_zone": "Remove a zone",
                "advanced": "Advanced",
                "finish": "Done",
            })
```

- [ ] **Step 5: Route the connect / bindings / weather sub-steps to the hub in options**

Replace the `if user_input is not None:` block of `async_step_connect` (lines 199-202) with:

```python
        if user_input is not None:
            self._secret_name = user_input["rachio_api_key_secret"]
            await self._connect_rachio(self._secret_name)
            if self._is_options:
                self._data["bindings"]["rachio_api_key_secret"] = self._secret_name
                self._persist()
                return await self.async_step_menu()
            return await self.async_step_bindings()
```

In `async_step_bindings`, replace the `else:` branch body (lines 308-318) with:

```python
            else:
                self._core = {
                    "notify_service": user_input["notify_service"],
                    "calendar_entity": user_input["calendar_entity"],
                    "rachio_device_name": user_input["rachio_device_name"],
                    "rachio_api_key_secret": self._secret_name,
                    "standby_switch": user_input["standby_switch"],
                    "forecast_entity": user_input["forecast_entity"],
                }
                await self._fetch_zones_for_device(user_input["rachio_device_name"])
                if self._is_options:
                    self._data["bindings"] = _assemble_bindings(
                        self._core, self._weather_form_from_bindings())
                    self._persist()
                    return await self.async_step_menu()
                return await self.async_step_weather()
```

In `async_step_weather`, replace the `if user_input is not None:` block (lines 326-332) with:

```python
        if user_input is not None:
            if self._is_options:
                self._data["bindings"] = _assemble_bindings(
                    self._core_from_bindings(), user_input)
                self._persist()
                return await self.async_step_menu()
            self._data["bindings"] = _assemble_bindings(self._core, user_input)
            return await self.async_step_zone()
```

- [ ] **Step 6: Route zone add/edit/remove to the hub with persistence**

In `async_step_confirm_remove`, replace the `if user_input is not None:` block (lines 424-428) with:

```python
        if user_input is not None:
            if user_input.get("confirm"):
                self._data["zones"] = [
                    z for z in self._data["zones"] if z["key"] != self._selected_key]
                self._persist()
            return await self.async_step_menu()
```

In `async_step_zone_details`, replace the edit branch (lines 539-549) with:

```python
            if self._editing_key:
                stored = self._stored_zone(self._editing_key)
                zid = (
                    self._picked_zone["id"] if self._picked_zone
                    else stored.get("rachio_zone_id", ""))
                self._data["zones"] = [
                    z for z in self._data["zones"] if z["key"] != self._editing_key]
                self._append_zone(dict(user_input, key=self._editing_key),
                                  rachio_zone_id=zid)
                self._editing_key = None
                self._persist()
                return await self.async_step_menu()
```

In `async_step_zone_details`, replace the add `else:` branch (lines 553-562) with:

```python
            else:
                zid = self._picked_zone["id"] if self._picked_zone else ""
                self._append_zone(user_input, rachio_zone_id=zid)
                if self._is_options:
                    # The hub is home base: adding returns there so the new zone
                    # is already persisted and the admin can add/edit/remove more.
                    self._persist()
                    return await self.async_step_menu()
                if user_input.get("add_another_zone"):
                    return await self.async_step_zone()
                return await self.async_step_advanced()
```

In `_async_step_zone_manual`, replace the `else:` branch (lines 622-628) with:

```python
            else:
                self._append_zone(user_input)
                if self._is_options:
                    self._persist()
                    return await self.async_step_menu()
                if user_input.get("add_another_zone"):
                    return await self.async_step_zone()
                return await self.async_step_advanced()
```

- [ ] **Step 7: Make `finish` save+exit and route `advanced` to the hub in options**

Replace `async_step_finish` (lines 398-399) with:

```python
    async def async_step_finish(self, user_input=None):
        # Options-hub "Done": persist, drop the reload guard, restart once.
        return await self._async_finish()
```

Replace `async_step_advanced` (lines 660-676) with (options branch persists and returns to the hub; config flow finishes as before — YAML validation is added in Task 3):

```python
    async def async_step_advanced(self, user_input=None):
        if user_input is not None:
            self._data["self_calibration_enabled"] = user_input["self_calibration_enabled"]
            self._data["advanced_overrides"] = user_input.get("advanced_overrides", "")
            if self._is_options:
                self._persist()
                return await self.async_step_menu()
            return await self._async_finish()

        schema = vol.Schema({
            vol.Optional(
                "self_calibration_enabled",
                default=self._existing.get("self_calibration_enabled", False),
            ): bool,
            vol.Optional(
                "advanced_overrides",
                default=self._existing.get("advanced_overrides", ""),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="advanced", data_schema=schema)
```

- [ ] **Step 8: Seed, suppress, connect, and land on the hub at options init; single reload at finish**

In `GeodropsRachioOptionsFlow`, replace `async_step_init` and `_async_finish` (lines 735-743) with:

```python
    async def async_step_init(self, user_input=None):
        self._existing = dict(self.config_entry.data)
        # Seed _data fully so any early sub-step persists a COMPLETE entry — an
        # unopened section (bindings, zones, calib, overrides) is kept verbatim.
        self._data["bindings"] = dict(self._existing.get("bindings", {}))
        self._data["zones"] = list(self._existing.get("zones", []))
        self._data["self_calibration_enabled"] = self._existing.get(
            "self_calibration_enabled", False)
        self._data["advanced_overrides"] = self._existing.get("advanced_overrides", "")
        # Defer scheduler restarts until "Done" (see __init__._reload_on_options).
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self.config_entry.entry_id, {})
        store["suppress_reload"] = True
        # Auto-connect with the stored secret so the Core device dropdown and
        # zone pickers are live without visiting Connect. Best-effort, degrades
        # exactly as the connect step does.
        self._secret_name = self._existing_bindings().get(
            "rachio_api_key_secret", DEFAULT_RACHIO_API_KEY_SECRET)
        await self._connect_rachio(self._secret_name)
        return await self.async_step_menu()

    async def _async_finish(self):
        entry = self.config_entry
        # Data is already persisted per-step; this is a no-op unless the admin
        # went straight to Done. Persist happens while the guard is still up.
        self._persist()
        store = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if store is not None:
            store["suppress_reload"] = False
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_create_entry(title="", data={})
```

- [ ] **Step 9: Update `strings.json` and `translations/en.json`**

In **both** files, replace the `options.step.manage_zones` block:

```json
      "manage_zones": {
        "title": "Manage Zones",
        "menu_options": {
          "add_zone": "Add a zone",
          "edit_zone": "Edit a zone",
          "remove_zone": "Remove a zone",
          "finish": "Done"
        }
      },
```

with:

```json
      "menu": {
        "title": "GeoDrops + Rachio Options",
        "menu_options": {
          "connect": "Connect Your Rachio Account",
          "bindings": "Core Setup",
          "weather": "Weather Station",
          "add_zone": "Add a zone",
          "edit_zone": "Edit a zone",
          "remove_zone": "Remove a zone",
          "advanced": "Advanced",
          "finish": "Done"
        }
      },
```

- [ ] **Step 10: Add the reload-through-flow test to `tests/test_init.py`**

This exercises the guard with a genuinely set-up entry (listener registered), unlike the config-flow tests which patch `async_reload`.

```python
async def test_options_flow_defers_reload_until_finish(hass, enable_pyscript_and_rachio):
    from unittest.mock import patch as _patch
    from custom_components.geodrops_rachio.const import DOMAIN
    zone = {"key": "front", "rachio_switch": "switch.front",
            "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
            "quality_sensors": [], "target_range": "moist",
            "runtime_minutes": 45, "refill_depth_mm": 7.11}
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"notify_service": "notify.phone", "rachio_device_name": "Main House"},
        "zones": [dict(zone)], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.delivery.async_deliver",
               AsyncMock(return_value=True)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reloads = []

        async def _fake_reload(entry_id):
            reloads.append(entry_id)

        async def _resolve(h, name):
            return None

        base = "custom_components.geodrops_rachio.config_flow."
        with _patch.object(hass.config_entries, "async_reload", _fake_reload), \
                _patch(base + "resolve_secret", _resolve):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "add_zone"})
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"key": "back", "rachio_switch": "switch.back",
                 "dominant_sensor": "sensor.d2", "state_sensor": "sensor.s2",
                 "quality_sensors": [], "target_range": "moist",
                 "runtime_minutes": 20, "refill_depth_mm": 10})
            await hass.async_block_till_done()
            # Persisted, but the guard held off the reload.
            assert [z["key"] for z in entry.data["zones"]] == ["front", "back"]
            assert reloads == []
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "finish"})
            await hass.async_block_till_done()
    assert reloads == [entry.entry_id]           # exactly one, at finish
```

- [ ] **Step 11: Run the full suite to verify green**

Run:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest tests/test_config_flow.py tests/test_init.py -q
```
Expected: PASS — all config-flow (first-install) tests unchanged and green, all rewritten/new options tests green, both init tests green.

- [ ] **Step 12: Commit**

```bash
git add custom_components/geodrops_rachio/config_flow.py custom_components/geodrops_rachio/strings.json custom_components/geodrops_rachio/translations/en.json tests/test_config_flow.py tests/test_init.py
git commit -m "feat(options): flat hub menu with persist-on-mutation

Options flow lands on a hub instead of re-walking the install wizard.
Each sub-step persists to the entry immediately; the reload is deferred
to a single scheduler restart on Done. First-install flow unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Validate advanced overrides before persisting

The options Advanced sub-step must reject invalid `advanced_overrides` YAML at submit, before `_persist()`, so a bad override can never be written to the entry (with the guarded reload it would otherwise only surface as `ConfigEntryNotReady` at the single "Done" reload). Reuse `config_writer.generate_config`, the single source of override validation.

**Files:**
- Modify: `custom_components/geodrops_rachio/config_flow.py` (`async_step_advanced`)
- Modify: `custom_components/geodrops_rachio/strings.json`, `translations/en.json` (`options.error.invalid_advanced_overrides`)
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `generate_config(data: dict) -> str` (imported in Task 2), which raises `ValueError` on invalid `advanced_overrides`.
- Produces: error key `invalid_advanced_overrides` on the `advanced` step's `advanced_overrides` field.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_flow.py`:

```python
async def test_options_advanced_invalid_yaml_rejected(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "advanced"})
        assert result["step_id"] == "advanced"
        # A non-mapping YAML scalar is invalid per generate_config.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"self_calibration_enabled": False, "advanced_overrides": "just a string"})
        assert result["step_id"] == "advanced"
        assert result["errors"] == {"advanced_overrides": "invalid_advanced_overrides"}
    # Nothing persisted.
    assert entry.data["advanced_overrides"] == ""


async def test_options_advanced_valid_yaml_persists(hass, enable_pyscript_and_rachio):
    entry = _options_entry(hass)
    with _patch_poll(key=None), _count_reloads(hass):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "advanced"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"self_calibration_enabled": True, "advanced_overrides": "rain_skip_mm: 2"})
        assert result["step_id"] == "menu"
    assert entry.data["self_calibration_enabled"] is True
    assert entry.data["advanced_overrides"] == "rain_skip_mm: 2"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest tests/test_config_flow.py::test_options_advanced_invalid_yaml_rejected -q
```
Expected: FAIL — Task 2's advanced step persists unconditionally and returns to `menu`, so the invalid string is accepted (`errors` empty, step is `menu`).

- [ ] **Step 3: Add validation to the options branch of `async_step_advanced`**

Replace `async_step_advanced` (as written in Task 2) with:

```python
    async def async_step_advanced(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            calib = user_input["self_calibration_enabled"]
            overrides = user_input.get("advanced_overrides", "")
            if self._is_options:
                trial = dict(
                    self._data, self_calibration_enabled=calib,
                    advanced_overrides=overrides)
                try:
                    # generate_config is the single source of override validation
                    # (raises ValueError on non-YAML / non-mapping overrides).
                    generate_config(trial)
                except ValueError:
                    errors["advanced_overrides"] = "invalid_advanced_overrides"
                else:
                    self._data["self_calibration_enabled"] = calib
                    self._data["advanced_overrides"] = overrides
                    self._persist()
                    return await self.async_step_menu()
            else:
                self._data["self_calibration_enabled"] = calib
                self._data["advanced_overrides"] = overrides
                return await self._async_finish()

        schema = vol.Schema({
            vol.Optional(
                "self_calibration_enabled",
                default=self._existing.get("self_calibration_enabled", False),
            ): bool,
            vol.Optional(
                "advanced_overrides",
                default=self._existing.get("advanced_overrides", ""),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        # On a validation re-render keep what the admin typed.
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="advanced", data_schema=schema, errors=errors)
```

- [ ] **Step 4: Add the error string to `strings.json` and `translations/en.json`**

In **both** files, in the `options.error` object, add the `invalid_advanced_overrides` entry alongside the existing keys:

```json
    "error": {
      "invalid_notify_service": "That target can't be called as a notify service. Pick a notify.mobile_app_* (or similar) service, not a notify entity.",
      "duplicate_zone_key": "A zone with this name already exists. Choose a different zone name.",
      "invalid_advanced_overrides": "Advanced overrides must be a valid YAML mapping of tunables. Fix the YAML and try again."
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest tests/test_config_flow.py tests/test_init.py -q
```
Expected: PASS — both new advanced tests green, full suite green.

- [ ] **Step 6: Verify strings/translations parity and run the whole project suite**

Run:
```bash
diff custom_components/geodrops_rachio/strings.json custom_components/geodrops_rachio/translations/en.json && echo IDENTICAL
MSYS_NO_PATHCONV=1 docker run --rm -v "/c/Users/Adam/Projects/Projects/ghr-options-hub:/app" -w /app ghr-test python -m pytest -q
```
Expected: `IDENTICAL`, then the entire suite passes.

- [ ] **Step 7: Commit**

```bash
git add custom_components/geodrops_rachio/config_flow.py custom_components/geodrops_rachio/strings.json custom_components/geodrops_rachio/translations/en.json tests/test_config_flow.py
git commit -m "feat(options): validate advanced_overrides YAML before persist

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Hub menu (§1): Task 2 Steps 4, 9 — `async_step_menu`, retitled "GeoDrops + Rachio Options", 8 menu items. ✔
- Seed `_data` at init (§2): Task 2 Step 8. ✔
- Edit independence (§2 note): Task 2 Step 3 (`_core_from_bindings`/`_weather_form_from_bindings`) + tests `test_options_bindings_prefills_and_edits_core_only`, `test_options_weather_edit_preserves_core`. ✔
- Auto-connect at init (§3): Task 2 Step 8 (`_connect_rachio(self._secret_name)`); Connect stays a menu item (Step 5, repoll persists secret) + test. ✔
- Persist-on-mutation + guarded reload (§4): Task 1 (listener) + Task 2 (`_persist` per step, `suppress_reload` set at init/cleared at finish, single reload) + tests `test_options_add_zone_persists_before_finish`, `test_options_flow_defers_reload_until_finish`. ✔
- Abandonment (§4): `test_options_add_zone_abandoned_still_persisted`. ✔
- First-install unchanged (§5): Global Constraints; config branch untouched; 14 existing config tests kept. ✔
- Advanced YAML validation (Error handling §217): Task 3. ✔
- Files touched table: all four production files + two test files covered. ✔
- Testing section bullets: navigation, seed, persist, guarded reload, abandonment, edit independence, config-flow-unchanged — all mapped to named tests. ✔

**Placeholder scan:** No TBD/"handle errors"/"similar to Task N"; every code step shows full code. ✔

**Type consistency:** `_persist`, `_core_from_bindings`, `_weather_form_from_bindings`, `async_step_menu`, `generate_config`, `suppress_reload`, `_secret_name`, `_data` keys (`bindings`/`zones`/`self_calibration_enabled`/`advanced_overrides`) used consistently across tasks. `async_step_manage_zones` removed and all call sites (weather, confirm_remove, zone_details ×2, zone_manual) redirected to `async_step_menu`. ✔
