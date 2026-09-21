import contextlib
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN


@pytest.fixture(autouse=True)
def _notify_service(hass):
    """The wizard validates notify_service against registered notify.* services;
    register the one the test inputs use so the bindings step accepts them."""
    hass.services.async_register("notify", "phone", lambda call: None)
    yield

CONNECT_INPUT = {"rachio_api_key_secret": "rachio_api_key"}

BINDINGS_INPUT = {
    "notify_service": "notify.phone",
    "calendar_entity": "calendar.lawn",
    "rachio_device_name": "Main House",
    "standby_switch": "switch.sprinkler_standby",
    "forecast_entity": "weather.home",
}

WEATHER_INPUT = {
    "weather_temperature": "sensor.tempest_sensor_temperature",
    "weather_humidity": "sensor.tempest_sensor_humidity",
    "weather_wind": "sensor.tempest_sensor_wind_speed_average",
    "weather_rain_last_hour": "sensor.tempest_rain_last_hour",
    "weather_precip_type": "sensor.tempest_sensor_precipitation_type",
    "precipitation_chance_prefix": "sensor.precipitation_chance_",
    "precipitation_amount_prefix": "sensor.precipitation_amount_",
}

ZONE_INPUT = {
    "key": "front",
    "rachio_switch": "switch.front",
    "dominant_sensor": "sensor.front_dom",
    "state_sensor": "sensor.front_state",
    "quality_sensors": ["sensor.front_q1"],
    "target_range": "moist",
    "runtime_minutes": 45,
    "refill_depth_mm": 7.11,
    "add_another_zone": False,
}

DEVICES = [{"id": "dev-1", "name": "Main House"}]
LIVE_ZONES = [
    {
        "id": "z-uuid-1",
        "name": "Front Yard",
        "zoneNumber": 1,
        "runtime_minutes": 30.0,
        "refill_depth_mm": 12.7,
    }
]


def _field_names(result):
    return {str(f) for f in result["data_schema"].schema}


def _patch_poll(*, key="KEY", devices=None, zones=None,
                devices_raise=False, zones_raise=False):
    """Patch the wizard's Rachio hooks. key=None simulates no resolvable
    secret; *_raise simulates an API error at that hop."""
    async def _resolve(hass, name):
        return key

    async def _fetch_devices(session, k):
        if devices_raise:
            raise RuntimeError("boom")
        return devices or []

    async def _fetch_zones(session, k, device_id):
        if zones_raise:
            raise RuntimeError("boom")
        return zones or []

    stack = contextlib.ExitStack()
    base = "custom_components.geodrops_rachio.config_flow."
    stack.enter_context(patch(base + "resolve_secret", _resolve))
    stack.enter_context(patch(base + "async_fetch_devices", _fetch_devices))
    stack.enter_context(patch(base + "async_fetch_device_zones", _fetch_zones))
    return stack


@contextlib.contextmanager
def _count_reloads(hass):
    """Patch async_reload to a counting no-op so options tests can run without
    setting up the entry and can assert the single 'Done' reload."""
    calls = []

    async def _fake_reload(entry_id):
        calls.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        yield calls


async def test_aborts_without_pyscript(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "missing_prerequisites"


async def test_happy_path_creates_entry(hass, enable_pyscript_and_rachio):
    # No resolvable key -> no live devices/zones -> free-text device + manual zone.
    with _patch_poll(key=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["step_id"] == "connect"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        assert result["step_id"] == "bindings"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        assert result["step_id"] == "weather"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
        assert result["step_id"] == "zone"
        assert "rachio_zone" not in _field_names(result)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ZONE_INPUT)
        assert result["step_id"] == "advanced"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"self_calibration_enabled": False})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    bindings = result["result"].data["bindings"]
    assert bindings["notify_service"] == "notify.phone"
    assert bindings["rachio_api_key_secret"] == "rachio_api_key"
    assert bindings["rachio_device_name"] == "Main House"
    assert bindings["drought_level_select"] == "select.geodrops_rachio_drought_level"
    assert bindings["sun"] == {
        "dawn": "sensor.sun_next_dawn", "sunrise": "sensor.sun_next_rising"}
    assert result["result"].data["zones"][0]["key"] == "front"


async def test_device_name_dropdown_and_live_zone_prefill(
        hass, enable_pyscript_and_rachio):
    with _patch_poll(key="KEY", devices=DEVICES, zones=LIVE_ZONES):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        assert result["step_id"] == "bindings"
        # device name is now a droppick defaulted to the fetched controller
        device_field = next(
            f for f in result["data_schema"].schema if f == "rachio_device_name")
        assert device_field.default() == "Main House"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)

        assert result["step_id"] == "zone"
        assert "rachio_zone" in _field_names(result)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"rachio_zone": "z-uuid-1"})

        assert result["step_id"] == "zone_details"
        defaults = {str(f): f.default() for f in result["data_schema"].schema
                    if callable(getattr(f, "default", None)) and f.default() is not None}
        assert defaults.get("runtime_minutes") == 30.0
        assert defaults.get("refill_depth_mm") == 12.7

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "key": "front",
                "rachio_switch": "switch.front",
                "dominant_sensor": "sensor.front_dom",
                "state_sensor": "sensor.front_state",
                "quality_sensors": ["sensor.front_q1"],
                "target_range": "moist",
                "runtime_minutes": 30.0,
                "refill_depth_mm": 12.7,
                "add_another_zone": False,
            })
        assert result["step_id"] == "advanced"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"self_calibration_enabled": False})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    zone = result["result"].data["zones"][0]
    assert zone["rachio_zone_id"] == "z-uuid-1"
    assert zone["runtime_minutes"] == 30.0


async def test_zone_details_prefills_matching_switch(
        hass, enable_pyscript_and_rachio):
    # A HA switch whose friendly name matches the Rachio zone -> pre-selected.
    hass.states.async_set(
        "switch.front_yard", "off", {"friendly_name": "Front Yard"})
    with _patch_poll(key="KEY", devices=DEVICES, zones=LIVE_ZONES):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"rachio_zone": "z-uuid-1"})
        assert result["step_id"] == "zone_details"
        switch_field = next(
            f for f in result["data_schema"].schema if f == "rachio_switch")
        assert switch_field.default() == "switch.front_yard"


async def test_zone_details_no_switch_match_requires_pick(
        hass, enable_pyscript_and_rachio):
    # No matching switch -> field has no default (user must pick).
    with _patch_poll(key="KEY", devices=DEVICES, zones=LIVE_ZONES):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"rachio_zone": "z-uuid-1"})
        switch_field = next(
            f for f in result["data_schema"].schema if f == "rachio_switch")
        # vol.Required with no default -> default is the UNDEFINED marker
        assert switch_field.default is vol.UNDEFINED


async def test_switch_match_normalizes_spaces_and_underscores(
        hass, enable_pyscript_and_rachio):
    # Rachio zone "Front Slope" should match a switch named "front_slope".
    hass.states.async_set(
        "switch.zone_ctrl", "off", {"friendly_name": "front_slope"})
    zones = [{
        "id": "z9", "name": "Front Slope", "zoneNumber": 2,
        "runtime_minutes": 20.0, "refill_depth_mm": 10.0,
    }]
    with _patch_poll(key="KEY", devices=DEVICES, zones=zones):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"rachio_zone": "z9"})
        switch_field = next(
            f for f in result["data_schema"].schema if f == "rachio_switch")
        assert switch_field.default() == "switch.zone_ctrl"


async def test_invalid_notify_service_shows_error(hass, enable_pyscript_and_rachio):
    # Picking a notify entity the scheduler can't call as a service is rejected.
    with _patch_poll(key=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        bad = dict(BINDINGS_INPUT, notify_service="notify.not_a_service")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], bad)
    assert result["step_id"] == "bindings"
    assert result["errors"] == {"notify_service": "invalid_notify_service"}


def _suggested(result, field):
    """The suggested_value carried on a re-rendered form field, if any."""
    for key in result["data_schema"].schema:
        if str(key) == field:
            return (getattr(key, "description", None) or {}).get("suggested_value")
    return None


async def test_duplicate_zone_key_preserves_typed_fields(
        hass, enable_pyscript_and_rachio):
    """A duplicate key re-renders the zone form with an error, keeping the other
    fields the user already typed instead of clearing them."""
    with _patch_poll(key=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
        assert result["step_id"] == "zone"
        # First zone; ask to add another so we return to the zone step.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ZONE_INPUT, "add_another_zone": True})
        assert result["step_id"] == "zone"
        # Second zone reuses the key but types different sensors.
        dup = {**ZONE_INPUT, "key": "front",
               "dominant_sensor": "sensor.other_dom", "add_another_zone": True}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], dup)
    assert result["step_id"] == "zone"
    assert result["errors"] == {"key": "duplicate_zone_key"}
    # The typed values survive the re-render as suggested values.
    assert _suggested(result, "dominant_sensor") == "sensor.other_dom"
    assert _suggested(result, "key") == "front"
    assert _suggested(result, "runtime_minutes") == 45


async def test_no_key_free_text_device_and_manual_zone(
        hass, enable_pyscript_and_rachio):
    with _patch_poll(key=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        # device name falls back to a free-text string field
        device_field = next(
            f for f in result["data_schema"].schema if f == "rachio_device_name")
        assert device_field.default() == ""
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
    assert result["step_id"] == "zone"
    fields = _field_names(result)
    assert "rachio_zone" not in fields
    assert "key" in fields and "runtime_minutes" in fields


async def test_zone_fetch_failure_falls_back_to_manual(
        hass, enable_pyscript_and_rachio):
    # devices fetch OK, but the per-device zone fetch fails -> manual zone form.
    with _patch_poll(key="KEY", devices=DEVICES, zones_raise=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BINDINGS_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
    assert result["step_id"] == "zone"
    assert "rachio_zone" not in _field_names(result)


async def test_device_fetch_failure_free_text_device(
        hass, enable_pyscript_and_rachio):
    with _patch_poll(key="KEY", devices_raise=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECT_INPUT)
        device_field = next(
            f for f in result["data_schema"].schema if f == "rachio_device_name")
        assert device_field.default() == ""



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
