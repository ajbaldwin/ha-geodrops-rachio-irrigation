import yaml
from custom_components.geodrops_rachio.config_writer import generate_config, GENERATED_HEADER

BASE = {
    "bindings": {
        "notify_service": "notify.phone",
        "calendar_entity": "calendar.lawn",
        "rachio_device_name": "Main House",
        "rachio_api_key_secret": "rachio_api_key",
    },
    "zones": [{
        "key": "front", "rachio_switch": "switch.front",
        "dominant_sensor": "sensor.front_dom", "state_sensor": "sensor.front_state",
        "quality_sensors": ["sensor.front_q1"], "target_range": "moist",
        "geography": "front", "adjacency": [], "runtime_minutes": 45,
        "refill_depth_mm": 7.11, "spray": False,
    }],
    "self_calibration_enabled": False,
    "advanced_overrides": "",
}


def test_header_present_and_flag_off():
    out = generate_config(BASE)
    assert out.startswith(GENERATED_HEADER)
    doc = yaml.safe_load(out)
    assert doc["tunables"]["self_calibration_enabled"] is False
    assert doc["zones"]["front"]["rachio_switch"] == "switch.front"


def test_flag_on_when_enabled():
    data = {**BASE, "self_calibration_enabled": True}
    doc = yaml.safe_load(generate_config(data))
    assert doc["tunables"]["self_calibration_enabled"] is True


def test_advanced_overrides_merge_last():
    data = {**BASE, "advanced_overrides": "probe_growth: 2.0\ncycle_minutes: 5"}
    doc = yaml.safe_load(generate_config(data))
    assert doc["tunables"]["probe_growth"] == 2.0
    assert doc["tunables"]["cycle_minutes"] == 5


def test_invalid_override_yaml_raises():
    import pytest
    data = {**BASE, "advanced_overrides": "probe_growth: : :"}
    with pytest.raises(ValueError):
        generate_config(data)


def test_non_mapping_override_raises():
    import pytest
    data = {**BASE, "advanced_overrides": "- a\n- b"}   # valid YAML, but a list
    with pytest.raises(ValueError):
        generate_config(data)
