from custom_components.geodrops_rachio.coordinator import (
    parse_last_nightly, parse_efficacy, parse_nightly_calibration)


def test_parse_last_nightly_extracts_zone_slice():
    attrs = {
        "delivered_minutes": {"front": 42.0, "back": 10.0},
        "watered": ["front"],
        # `end` is time-only and unparseable as a timestamp; `updated` carries
        # the run's full ISO time and is what last_watered uses.
        "end": "06:00",
        "updated": "2026-09-13T06:00:00+00:00",
    }
    out = parse_last_nightly(attrs, "front")
    assert out["last_delivered_runtime"] == 42.0
    assert out["last_watered"] == "2026-09-13T06:00:00+00:00"

    # A zone that didn't water: delivered unknown, no watered timestamp.
    out_b = parse_last_nightly(attrs, "back")
    assert out_b["last_delivered_runtime"] == 10.0
    assert out_b["last_watered"] is None


def test_parse_nightly_calibration_extracts_zone_state():
    attrs = {"calibration": {"front": {"state": "calibrating", "efficacy": 0.35}}}
    assert parse_nightly_calibration(attrs, "front") == {
        "efficacy": 0.35, "calibration_state": "calibrating"}
    # A zone not in this night's calibration block -> both None.
    assert parse_nightly_calibration(attrs, "back") == {
        "efficacy": None, "calibration_state": None}
    assert parse_nightly_calibration({}, "front") == {
        "efficacy": None, "calibration_state": None}


def test_parse_last_nightly_missing_keys_are_none():
    assert parse_last_nightly({}, "front") == {
        "last_delivered_runtime": None, "last_watered": None}


def test_parse_efficacy_extracts_zone():
    store = {"front": {"efficacy": 0.42, "state": "converged"}}
    assert parse_efficacy(store, "front") == {
        "efficacy": 0.42, "calibration_state": "converged"}
    assert parse_efficacy(store, "missing") == {
        "efficacy": None, "calibration_state": None}


def test_remove_listener_stops_callbacks():
    from custom_components.geodrops_rachio.coordinator import ZoneStateCoordinator
    c = ZoneStateCoordinator(None, None)
    calls = []
    cb = lambda: calls.append(1)
    c.add_listener(cb)
    c._notify()
    assert calls == [1]
    c.remove_listener(cb)
    c._notify()
    assert calls == [1]  # no further callbacks after removal
    c.remove_listener(lambda: None)  # removing an unknown cb is a no-op
