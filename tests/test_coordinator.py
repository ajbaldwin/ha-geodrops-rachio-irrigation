from custom_components.geodrops_rachio.coordinator import (
    parse_last_nightly, parse_efficacy, parse_nightly_calibration,
    parse_refill_depth)


def test_parse_refill_depth_prefers_live_then_static():
    attrs = {"refill_depths_mm": {"zone-abc": 17.5}}
    # Live value for a mapped zone wins over the static config value.
    assert parse_refill_depth(attrs, "zone-abc", 10.0) == {"refill_depth": 17.5}
    # Zone not in the live payload -> static config value.
    assert parse_refill_depth(attrs, "zone-xyz", 10.0) == {"refill_depth": 10.0}
    # No rachio_zone_id (manual zone) -> static config value.
    assert parse_refill_depth(attrs, "", 10.0) == {"refill_depth": 10.0}
    # No runtimes entity / no live payload -> static config value.
    assert parse_refill_depth({}, "zone-abc", 10.0) == {"refill_depth": 10.0}
    # Nothing anywhere -> None (sensor reads unknown).
    assert parse_refill_depth({}, "", None) == {"refill_depth": None}


def test_parse_last_nightly_extracts_zone_slice():
    attrs = {
        "delivered_minutes": {"front": 42.0, "back": 10.0},
        "watered": ["front"],
        # `end` is time-only and unparseable as a timestamp. `end_iso` is the
        # real tz-aware valve-close instant and is what last_watered uses;
        # `updated` (plan-publish time) is only the version-skew fallback.
        "end": "06:44",
        "end_iso": "2026-09-13T06:44:03-04:00",
        "updated": "2026-09-13T06:00:00",
    }
    out = parse_last_nightly(attrs, "front")
    assert out["last_delivered_runtime"] == 42.0
    assert out["last_watered"] == "2026-09-13T06:44:03-04:00"

    # A zone that didn't water: delivered unknown, no watered timestamp.
    out_b = parse_last_nightly(attrs, "back")
    assert out_b["last_delivered_runtime"] == 10.0
    assert out_b["last_watered"] is None


def test_parse_last_nightly_falls_back_to_updated_without_end_iso():
    # Version skew: a new wrapper reading a record from a scheduler that predates
    # end_iso (or a no-water night, end_iso == "") falls back to `updated`.
    attrs = {"watered": ["front"], "end": "06:44",
             "updated": "2026-09-13T06:00:00"}
    assert parse_last_nightly(attrs, "front")["last_watered"] == "2026-09-13T06:00:00"
    empty = {"watered": ["front"], "end_iso": "", "updated": "2026-09-13T06:00:00"}
    assert parse_last_nightly(empty, "front")["last_watered"] == "2026-09-13T06:00:00"


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
