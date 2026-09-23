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


def test_emits_bands_defaults():
    doc = yaml.safe_load(generate_config(BASE))
    assert doc["bands"] == {
        "dry": {"low": 0, "high": 62},
        "dry_plus": {"low": 62, "high": 67},
        "moist": {"low": 67, "high": 76},
        "moist_plus": {"low": 76, "high": 87},
        "wet": {"low": 87, "high": 95},
        "wet_plus": {"low": 95, "high": 100},
    }


def test_emits_drought_profiles_defaults():
    doc = yaml.safe_load(generate_config(BASE))
    profiles = doc["drought_profiles"]
    assert set(profiles) == {
        "Level 0 - Normal", "Level 1 - Mild", "Level 2 - Significant",
        "Level 3 - Critical", "Level 4 - Emergency",
    }
    assert profiles["Level 0 - Normal"] == {
        "target_offset": 0, "trigger_margin": 0, "runtime_scale": 1.0,
        "rain_skip_horizon_hours": 12, "end_anchor": "sunrise",
    }
    assert profiles["Level 4 - Emergency"] == {
        "target_offset": -2, "trigger_margin": 0, "runtime_scale": 0.5,
        "rain_skip_horizon_hours": 24, "end_anchor": "dawn",
    }


def test_advanced_overrides_do_not_touch_bands_or_profiles():
    data = {**BASE, "advanced_overrides": "cycle_minutes: 7"}
    doc = yaml.safe_load(generate_config(data))
    assert doc["tunables"]["cycle_minutes"] == 7
    # bands come from the defaults regardless of overrides; drought_profiles are
    # untouched unless a `drought_profiles:` override key is provided.
    assert doc["bands"]["moist"] == {"low": 67, "high": 76}
    assert doc["drought_profiles"]["Level 3 - Critical"]["end_anchor"] == "dawn"


def test_drought_profile_override_merges_per_level_keeping_defaults():
    # A partial per-level override adds/overrides only the given keys and leaves
    # the rest of that level's defaults intact.
    data = {**BASE, "advanced_overrides": (
        "drought_profiles:\n"
        '  "Level 0 - Normal": {end_offset_minutes: -60}\n'
        '  "Level 1 - Mild": {end_offset_minutes: -30}\n'
        '  "Level 2 - Significant": {end_offset_minutes: -15}\n'
    )}
    doc = yaml.safe_load(generate_config(data))
    profiles = doc["drought_profiles"]
    assert profiles["Level 0 - Normal"]["end_offset_minutes"] == -60
    assert profiles["Level 1 - Mild"]["end_offset_minutes"] == -30
    assert profiles["Level 2 - Significant"]["end_offset_minutes"] == -15
    # untouched defaults on an overridden level survive
    assert profiles["Level 0 - Normal"]["end_anchor"] == "sunrise"
    assert profiles["Level 0 - Normal"]["runtime_scale"] == 1.0
    # a level not mentioned in the override is unchanged
    assert profiles["Level 3 - Critical"] == {
        "target_offset": -1, "trigger_margin": 6, "runtime_scale": 0.7,
        "rain_skip_horizon_hours": 24, "end_anchor": "dawn",
    }


def test_drought_profile_override_does_not_leak_into_tunables():
    data = {**BASE, "advanced_overrides": (
        "cycle_minutes: 12\n"
        "drought_profiles:\n"
        '  "Level 0 - Normal": {end_offset_minutes: -60}\n'
    )}
    doc = yaml.safe_load(generate_config(data))
    # the drought_profiles override goes to drought_profiles, not tunables
    assert "drought_profiles" not in doc["tunables"]
    assert doc["tunables"]["cycle_minutes"] == 12
    assert doc["drought_profiles"]["Level 0 - Normal"]["end_offset_minutes"] == -60


def test_drought_profile_override_non_mapping_raises():
    import pytest
    data = {**BASE, "advanced_overrides": "drought_profiles: not-a-mapping"}
    with pytest.raises(ValueError):
        generate_config(data)


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


def test_build_config_matches_generated_yaml():
    from custom_components.geodrops_rachio.config_writer import build_config
    assert build_config(BASE) == yaml.safe_load(generate_config(BASE))


def test_build_config_parses_with_brain():
    from custom_components.geodrops_rachio.brain import config as brain_config
    from custom_components.geodrops_rachio.config_writer import build_config
    cfg = brain_config.parse_config(build_config(BASE))
    assert cfg.zones["front"].rachio_switch == "switch.front"
    assert cfg.bindings.notify_service == "notify.phone"


def test_build_config_returns_fresh_dict_each_call():
    from custom_components.geodrops_rachio.config_writer import build_config
    first = build_config(BASE)
    first["zones"]["front"]["rachio_switch"] = "mutated"
    assert build_config(BASE)["zones"]["front"]["rachio_switch"] == "switch.front"
