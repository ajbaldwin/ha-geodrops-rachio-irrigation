from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

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


def _patch_poll(*, key="KEY", zones=None, fetch_raises=False):
    """Patch the wizard's Rachio auto-poll hooks.

    key=None simulates no resolvable secret; fetch_raises simulates an API
    error. Returns a context-manager stack entered by the caller via `with`.
    """
    import contextlib

    async def _fake_resolve(hass, name):
        return key

    async def _fake_fetch(session, k):
        if fetch_raises:
            raise RuntimeError("boom")
        return zones or []

    stack = contextlib.ExitStack()
    stack.enter_context(patch(
        "custom_components.geodrops_rachio.config_flow.resolve_secret",
        _fake_resolve))
    stack.enter_context(patch(
        "custom_components.geodrops_rachio.config_flow.async_fetch_zones",
        _fake_fetch))
    return stack

CORE_INPUT = {
    "notify_service": "notify.phone",
    "calendar_entity": "calendar.lawn",
    "rachio_device_name": "Main House",
    "rachio_api_key_secret": "rachio_api_key",
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


async def test_aborts_without_pyscript(hass):
    # pyscript/rachio not loaded in the test env -> prereq step aborts
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "missing_prerequisites"


async def test_happy_path_creates_entry(hass, enable_pyscript_and_rachio):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "bindings"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CORE_INPUT)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "weather"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], WEATHER_INPUT)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "zone"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ZONE_INPUT)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"self_calibration_enabled": False})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    bindings = result["result"].data["bindings"]
    # user-collected value made it through
    assert bindings["notify_service"] == "notify.phone"
    assert bindings["forecast_entity"] == "weather.home"
    # fixed, integration-owned constant from the addendum was assembled in
    assert bindings["drought_level_select"] == "select.geodrops_rachio_drought_level"
    assert bindings["standby_boolean"] == "switch.geodrops_rachio_standby"
    assert bindings["dew_formed_boolean"] == "switch.geodrops_rachio_dew_formed"
    assert bindings["run_active_boolean"] == "switch.geodrops_rachio_run_active"
    assert bindings["sun"] == {
        "dawn": "sensor.sun_next_dawn", "sunrise": "sensor.sun_next_rising"}
    assert bindings["derived"]["forecast_overnight"]["temp"] == (
        "sensor.geodrops_rachio_forecast_overnight_temp")
    assert bindings["derived"]["precipitation_chance_prefix"] == (
        "sensor.precipitation_chance_")
    assert bindings["weather"]["temperature"] == "sensor.tempest_sensor_temperature"

    zones = result["result"].data["zones"]
    assert zones[0]["key"] == "front"
    assert result["result"].data["self_calibration_enabled"] is False


async def test_live_zones_prefill_and_persist_zone_id(hass, enable_pyscript_and_rachio):
    with _patch_poll(key="KEY", zones=LIVE_ZONES):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CORE_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)

        # zone step is now a Rachio-zone picker, not the manual form
        assert result["step_id"] == "zone"
        assert "rachio_zone" in _field_names(result)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"rachio_zone": "z-uuid-1"})
        # details step is pre-filled from the picked zone
        assert result["step_id"] == "zone_details"
        defaults = {str(f): f.default() for f in result["data_schema"].schema
                    if f.default is not None and callable(getattr(f, "default", None))}
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
    assert zone["key"] == "front"
    assert zone["rachio_zone_id"] == "z-uuid-1"
    assert zone["runtime_minutes"] == 30.0


async def test_no_key_falls_back_to_manual_zone(hass, enable_pyscript_and_rachio):
    with _patch_poll(key=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CORE_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
    # No resolvable key -> today's manual zone form (has `key`, no `rachio_zone`)
    assert result["step_id"] == "zone"
    fields = _field_names(result)
    assert "rachio_zone" not in fields
    assert "key" in fields and "runtime_minutes" in fields


async def test_fetch_failure_falls_back_to_manual_zone(hass, enable_pyscript_and_rachio):
    with _patch_poll(key="KEY", fetch_raises=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CORE_INPUT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEATHER_INPUT)
    assert result["step_id"] == "zone"
    assert "rachio_zone" not in _field_names(result)


async def test_options_flow_prefills_and_preserves_zones(hass, enable_pyscript_and_rachio):
    original_bindings = {
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
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "bindings": original_bindings,
            "zones": [dict(ZONE_INPUT, add_another_zone=None)],
            "self_calibration_enabled": False,
            "advanced_overrides": "",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "bindings"
    # pre-filled with the existing value
    notify_field = next(
        f for f in result["data_schema"].schema if f == "notify_service")
    assert notify_field.default() == "notify.phone"

    # accept the bindings/weather pre-filled defaults unchanged
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], CORE_INPUT)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], WEATHER_INPUT)
    assert result["step_id"] == "zone_gate"

    # decline to touch zones -> existing zone is preserved untouched
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"add_or_edit_zones": False})
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"self_calibration_enabled": True})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert entry.data["zones"][0]["key"] == "front"
    assert entry.data["self_calibration_enabled"] is True
    assert entry.data["bindings"]["drought_level_select"] == (
        "select.geodrops_rachio_drought_level")
