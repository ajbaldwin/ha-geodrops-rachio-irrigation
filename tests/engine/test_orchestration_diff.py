import datetime as dt
import logging
import random

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.diff import (
    ENGINE_LOGGER_PREFIX, assert_same_effects, log_trail_legacy, log_trail_native,
)
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import ALL_MIXINS, T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


CAL = {"irrigation_efficacy.json": {"front": {"state": "calibrating"},
                                    "back": {"state": "calibrating"}}}


def _set(entity, value):
    return lambda w, eng=None: w.set(entity, value)


# name -> (self_cal, seed files, [(time, action)], wait, trigger)
# action(world, target) where target is the legacy ns (dict) or the native engine.
SCENARIOS = {
    "normal_night": (False, {}, [], True, "nightly"),
    "standby": (False, {}, [("2026-07-01 22:59:59", _set("switch.geodrops_rachio_standby", "on"))], True, "nightly"),
    "rain_skip_at_plan": (False, {}, [
        ("2026-07-01 22:59:59", _set("sensor.precipitation_chance_18_hour", "90")),
        ("2026-07-01 22:59:59", _set("sensor.precipitation_amount_18_hour", "25"))], True, "nightly"),
    "rain_skip_at_window_start": (False, {}, [
        ("2026-07-02 01:00:00", _set("sensor.precipitation_chance_18_hour", "90")),
        ("2026-07-02 01:00:00", _set("sensor.precipitation_amount_18_hour", "25"))], True, "nightly"),
    "moisture_risen": (False, {}, [("2026-07-02 01:00:00", _set("sensor.front_dominant", "80.0"))], True, "nightly"),
    "all_moisture_risen": (False, {}, [
        ("2026-07-02 01:00:00", _set("sensor.front_dominant", "80.0")),
        ("2026-07-02 01:00:00", _set("sensor.back_dominant", "80.0"))], True, "nightly"),
    "sensor_recovery_probe": (True, CAL, [
        ("2026-07-01 22:59:59", _set("sensor.back_q1", "Bad")),
        ("2026-07-01 22:59:59", _set("sensor.back_q2", "Bad")),
        ("2026-07-02 00:10:00", _set("sensor.back_q1", "Good")),
        ("2026-07-02 00:10:00", _set("sensor.back_q2", "Good"))], True, "nightly"),
    "run_now": (False, {}, [], False, "run_now"),
    "hail_abort_mid_run": (False, {}, [("2026-07-02 04:10:00", _set(
        "sensor.tempest_sensor_precipitation_type", "hail"))], True, "nightly"),
    "notify_and_calendar_fail": (False, {}, [], True, "nightly"),
    "preview_during_wait": (False, {}, [("2026-07-02 01:00:00", "preview")], True, "nightly"),
}


def _schedule(world, events, target):
    for when, action in events:
        if action == "preview":
            fn = (lambda t=target: t["_preview"]()) if isinstance(target, dict) \
                else (lambda t=target: t._preview())
        else:
            fn = (lambda a=action: a(world))
        world.at(when, fn)


def _prepare(freezer, name, data):
    freezer.move_to("2026-07-01 22:59:58")
    w = FakeWorld(freezer)
    populate(w, data)
    if name == "notify_and_calendar_fail":
        w.failing |= {("notify", "phone"), ("calendar", "create_event")}
    return w


def _legacy_last_run(lw):
    return lw.published["pyscript.geodrops_rachio_last_run"]


def _assert_branch(name, lw):
    """Pin, on the LEGACY side, that each scenario reaches the branch it is
    named for — so no scenario silently degrades to `normal_night` and passes
    the differential comparison vacuously."""
    value, attrs = _legacy_last_run(lw)
    nightly = lw.published.get("pyscript.geodrops_rachio_last_nightly")
    preview = lw.published.get("pyscript.geodrops_rachio_preview")
    warnings = [m for lvl, m in lw.logs if lvl == "warning"]
    if name == "normal_night":
        assert "skipped" not in attrs
        assert attrs["aborted_reason"] is None
        assert value == 2 and sorted(attrs["watered"]) == ["back", "front"]
        assert "window_start_dropped" not in attrs
        assert "recovery_added" not in attrs
        assert nightly is not None and preview is None
    elif name == "standby":
        assert attrs["skipped"] == "standby" and value == 0
    elif name == "rain_skip_at_plan":
        assert attrs["skipped"] == "rain-forecast" and value == 0
    elif name == "rain_skip_at_window_start":
        assert attrs["skipped"] == "rain-forecast-at-window-start" and value == 0
    elif name == "moisture_risen":
        assert "skipped" not in attrs
        assert list(attrs["window_start_dropped"]) == ["front"]
        assert attrs["uncompleted"].get("front") == "moisture-risen"
        assert attrs["watered"] == ["back"]
    elif name == "all_moisture_risen":
        assert attrs["skipped"] == "moisture-risen" and value == 0
        assert sorted(attrs["window_start_dropped"]) == ["back", "front"]
    elif name == "sensor_recovery_probe":
        assert list(attrs["recovery_added"]) == ["back"]
        assert "back" in attrs["watered"]
        assert attrs["dosing_sources"]["back"] == "probe"
    elif name == "run_now":
        assert attrs["trigger"] == "run_now" and value == 2
        assert nightly is None            # a run_now never overwrites the nightly record
    elif name == "hail_abort_mid_run":
        # The rain abort's reason string is "rain-abort" (brain/abort.py).
        assert attrs["aborted_reason"] == "rain-abort"
        assert attrs["breadcrumbs"][-1]["event"] == "give-up"
        # The hail lands inside the watering span, not before it.
        started = dt.datetime.strptime(attrs["start"], "%H:%M").time()
        assert started < dt.time(4, 10)
    elif name == "notify_and_calendar_fail":
        assert any("notification failed" in m for m in warnings)
        assert any("calendar entry failed" in m for m in warnings)
        assert value == 2 and attrs["aborted_reason"] is None
    elif name == "preview_during_wait":
        assert preview is not None and preview[0] == 2
        assert preview[1]["updated"] == "2026-07-02T01:00:00"
        assert value == 2 and attrs["aborted_reason"] is None
    else:  # pragma: no cover - every scenario must be pinned
        raise AssertionError(f"no branch evidence for {name}")


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_night_matches_legacy(freezer, name, caplog):
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)
    self_cal, files, events, wait, trigger = SCENARIOS[name]
    data = entry_data(self_cal=self_cal)

    lw = _prepare(freezer, name, data)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    _schedule(lw, events, ns)
    lw.advance(2)                      # fire the 22:59:59 setup events; now 23:00:00
    ns["_plan_and_run"](wait, trigger)

    nw = _prepare(freezer, name, data)
    eng = native_engine(nw, data, *ALL_MIXINS,
                        docs={LEGACY_FILE_KEYS[f]: v for f, v in files.items()})
    _schedule(nw, events, eng)
    for coro in nw.advance(2):
        await coro
    await eng._plan_and_run(wait, trigger)

    assert_same_effects(lw, lf, nw, eng)
    assert log_trail_native(caplog) == log_trail_legacy(lw)
    _assert_branch(name, lw)


async def test_preview_refused_while_watering(freezer):
    data = entry_data()
    w = _prepare(freezer, "x", data)
    eng = native_engine(w, data, *ALL_MIXINS)
    eng._current_bindings = eng._load_cfg().bindings
    eng._watering_active = True
    await eng._preview()
    assert "preview" not in eng.records
    assert w.calls[-1][2]["message"] == "Preview skipped: watering in progress"
