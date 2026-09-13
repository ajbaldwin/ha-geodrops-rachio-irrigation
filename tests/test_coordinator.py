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
