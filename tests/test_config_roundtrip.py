"""End-to-end: the wizard's generated config must load in the REAL scheduler.

The mocked suite never fed generate_config's output through the actual
scheduler parser, which is exactly why C1 (missing bands/drought_profiles)
slipped through. This test closes that gap: it assembles a realistic
ConfigEntry data dict the way the config flow does, runs it through
generate_config, then parses the result with the VENDORED scheduler's
geodrops_rachio_lib.config — the same code that will run on the box.
"""
import pathlib
import sys

import yaml

from custom_components.geodrops_rachio.config_writer import generate_config
from custom_components.geodrops_rachio.config_flow import (
    _assemble_bindings,
    FIXED_BINDINGS,
    FIXED_DERIVED,
    SUN_DEFAULTS,
)

# Import the VENDORED scheduler lib (the bytes that ship on the box), not the
# canonical source tree. geodrops_rachio_lib is pure Python (dataclasses + yaml, no
# pyscript globals), so it imports fine once its parent dir is on sys.path.
_BUNDLED = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components" / "geodrops_rachio" / "bundled_app"
)
if str(_BUNDLED) not in sys.path:
    sys.path.insert(0, str(_BUNDLED))

from geodrops_rachio_lib import config as scheduler_config  # noqa: E402


def _wizard_data() -> dict:
    """A ConfigEntry data dict shaped exactly like the config flow persists."""
    core = {
        "notify_service": "notify.mobile_app_phone",
        "calendar_entity": "calendar.lawn",
        "rachio_api_key_secret": "rachio_api_key",
        "rachio_device_name": "Main House",
        "standby_switch": "switch.sprinkler_standby",
        "forecast_entity": "weather.home",
    }
    weather = {
        "weather_temperature": "sensor.tempest_sensor_temperature",
        "weather_humidity": "sensor.tempest_sensor_humidity",
        "weather_wind": "sensor.tempest_sensor_wind_speed_average",
        "weather_rain_last_hour": "sensor.tempest_rain_last_hour",
        "weather_precip_type": "sensor.tempest_sensor_precipitation_type",
        "precipitation_chance_prefix": "sensor.precipitation_chance_",
        "precipitation_amount_prefix": "sensor.precipitation_amount_",
    }
    return {
        "bindings": _assemble_bindings(core, weather),
        "zones": [
            {
                "key": "front",
                "rachio_switch": "switch.front",
                "dominant_sensor": "sensor.front_dom",
                "state_sensor": "sensor.front_state",
                "quality_sensors": ["sensor.front_q1", "sensor.front_q2"],
                "target_range": "moist",
                "geography": "front",
                "adjacency": [],
                "runtime_minutes": 45,
                "refill_depth_mm": 7.11,
                "spray": False,
            },
            {
                "key": "back",
                "rachio_switch": "switch.back",
                "dominant_sensor": "sensor.back_dom",
                "state_sensor": "sensor.back_state",
                "quality_sensors": ["sensor.back_q1"],
                "target_range": "moist",
                "geography": "back",
                "adjacency": ["front"],
                "runtime_minutes": 60,
                "refill_depth_mm": 8.89,
                "spray": True,
            },
        ],
        "self_calibration_enabled": False,
        "advanced_overrides": "",
    }


def test_generated_config_parses_in_vendored_scheduler():
    raw = yaml.safe_load(generate_config(_wizard_data()))

    # parse_config direct-indexes raw["bands"], raw["drought_profiles"],
    # raw["zones"][*] — a missing section is a KeyError on a real run.
    cfg = scheduler_config.parse_config(raw)

    # Bands and drought profiles must be present and complete.
    assert set(cfg.bands) == {
        "dry", "dry_plus", "moist", "moist_plus", "wet", "wet_plus"
    }
    assert "Level 3 - Critical" in cfg.drought_profiles
    assert cfg.drought_profiles["Level 3 - Critical"].end_anchor == "dawn"

    # Zones parsed with their runtimes intact.
    assert set(cfg.zones) == {"front", "back"}
    assert cfg.zones["front"].runtime_minutes == 45.0
    assert cfg.zones["back"].spray is True


def test_bindings_resolve_fixed_entity_ids():
    raw = yaml.safe_load(generate_config(_wizard_data()))
    bindings = scheduler_config.parse_bindings(raw)

    # The integration-owned fixed entity ids must survive the round trip.
    assert bindings.drought_level_select == "select.geodrops_rachio_drought_level"
    assert bindings.standby_boolean == "switch.geodrops_rachio_standby"
    assert bindings.dew_formed_boolean == "switch.geodrops_rachio_dew_formed"
    assert bindings.run_active_boolean == "switch.geodrops_rachio_run_active"

    # User-collected bindings and derived sensors resolve too.
    assert bindings.notify_service == "notify.mobile_app_phone"
    assert bindings.rachio_device_name == "Main House"
    assert (
        bindings.derived.forecast_overnight["temp"]
        == "sensor.geodrops_rachio_forecast_overnight_temp"
    )


def test_fixed_constants_are_wired_through():
    """Guards against the config flow constants drifting from the schema."""
    raw = yaml.safe_load(generate_config(_wizard_data()))
    ha = raw["homeassistant"]
    for key, value in FIXED_BINDINGS.items():
        assert ha[key] == value
    assert ha["sun"] == SUN_DEFAULTS
    assert ha["derived"]["forecast_overnight"] == FIXED_DERIVED["forecast_overnight"]
