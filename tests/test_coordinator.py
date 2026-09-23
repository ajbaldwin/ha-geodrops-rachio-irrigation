from types import SimpleNamespace

from custom_components.geodrops_rachio.coordinator import (
    ZoneStateCoordinator, parse_last_nightly, parse_efficacy,
    parse_nightly_calibration, parse_refill_depth, format_calibration_status)
from custom_components.geodrops_rachio.engine.store import EngineStore


async def _nosave(_docs):
    return None


def _scheduler_stub(records=None, docs=None):
    return SimpleNamespace(records=records or {},
                           store=EngineStore(docs or {}, _nosave),
                           add_listener=lambda cb: (lambda: None))


def test_format_calibration_status():
    # Converged: just the label.
    assert format_calibration_status("converged", 3, None) == "Converged"
    # A reject reason wins over the count — it explains why probes aren't adding up.
    assert format_calibration_status("calibrating", 1, "saturated") == \
        "Calibrating — soil too wet"
    assert format_calibration_status("calibrating", 0, "no_rise") == \
        "Calibrating — probe too small"
    assert format_calibration_status("recalibrating", 0, "rain") == \
        "Recalibrating — rained out"
    # No reject, some accepted probes -> progress out of the convergence target.
    assert format_calibration_status("calibrating", 2, None) == "Calibrating (2/3)"
    # No reject, no probes yet -> plain label.
    assert format_calibration_status("calibrating", 0, None) == "Calibrating"
    # Missing state passes through.
    assert format_calibration_status(None, 0, None) is None


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
    attrs = {"calibration": {"front": {"state": "calibrating", "efficacy": 0.35,
                                       "n_obs": 2, "last_reject_reason": "no_rise"}}}
    assert parse_nightly_calibration(attrs, "front") == {
        "efficacy": 0.35, "calibration_state": "calibrating",
        "n_obs": 2, "last_reject_reason": "no_rise"}
    # A zone not in this night's calibration block -> state/efficacy/reason None.
    assert parse_nightly_calibration(attrs, "back") == {
        "efficacy": None, "calibration_state": None,
        "n_obs": 0, "last_reject_reason": None}
    assert parse_nightly_calibration({}, "front") == {
        "efficacy": None, "calibration_state": None,
        "n_obs": 0, "last_reject_reason": None}


def test_parse_last_nightly_missing_keys_are_none():
    assert parse_last_nightly({}, "front") == {
        "last_delivered_runtime": None, "last_watered": None}


def test_parse_efficacy_extracts_zone():
    store = {"front": {"efficacy": 0.42, "state": "converged",
                       "n_obs": 3, "last_reject_reason": None}}
    assert parse_efficacy(store, "front") == {
        "efficacy": 0.42, "calibration_state": "converged",
        "n_obs": 3, "last_reject_reason": None}
    assert parse_efficacy(store, "missing") == {
        "efficacy": None, "calibration_state": None,
        "n_obs": 0, "last_reject_reason": None}


def test_remove_listener_stops_callbacks():
    c = ZoneStateCoordinator(None, None, _scheduler_stub())
    calls = []
    cb = lambda: calls.append(1)
    c.add_listener(cb)
    c._notify()
    assert calls == [1]
    c.remove_listener(cb)
    c._notify()
    assert calls == [1]  # no further callbacks after removal
    c.remove_listener(lambda: None)  # removing an unknown cb is a no-op


def test_data_for_reads_scheduler_records(hass):
    entry = SimpleNamespace(data={"zones": [{"key": "front", "rachio_zone_id": "z1",
                                             "refill_depth_mm": 7.0}]})
    sched = _scheduler_stub(
        records={
            "last_nightly": {"value": 1, "attributes": {
                "watered": ["front"], "delivered_minutes": {"front": 42.0},
                "end_iso": "2026-09-13T06:00:00+00:00",
                "calibration": {"front": {"state": "calibrating", "n_obs": 2}}}},
            "targets": {"value": 1, "attributes": {"target_floors": {"front": 65.0}}},
            "runtimes": {"value": 1, "attributes": {"refill_depths_mm": {"z1": 8.5}}},
            "preview": {"value": 1, "attributes": {"planned_minutes": {"front": 30}}},
        },
        docs={"efficacy": {"front": {"efficacy": 0.4}}})
    out = ZoneStateCoordinator(hass, entry, sched).data_for("front")
    assert out["last_delivered_runtime"] == 42.0
    assert out["target_floor"] == 65.0
    assert out["refill_depth"] == 8.5
    assert out["planned_runtime"] == 30
    assert out["efficacy"] == 0.4
    assert out["calibration_state"] == "Calibrating (2/3)"
