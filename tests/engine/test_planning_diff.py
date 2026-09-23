import logging
import random

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from tests.engine.diff import (
    ENGINE_LOGGER_PREFIX, legacy_calls, log_trail_legacy, log_trail_native, prime,
)
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


EFFICACY_CAL = {"front": {"state": "calibrating"}, "back": {"state": "converged",
                "efficacy": 0.5, "span_pts": 12.0}}

SCENARIOS = {
    "normal": (False, {}, lambda w: None),
    "excluded": (False, {}, lambda w: w.set("switch.geodrops_rachio_back_exclude", "on")),
    "low_quality": (False, {}, lambda w: w.set("sensor.front_q1", "Bad") or w.set("sensor.front_q2", "Bad")),
    "forecast_missing": (False, {}, lambda w: w.remove("sensor.forecast_overnight_temp")),
    "one_zone_wet": (False, {}, lambda w: w.set("sensor.back_dominant", "80.0")),
    "probe_calibrating": (True, {"irrigation_efficacy.json": EFFICACY_CAL},
                          lambda w: w.set("sensor.front_dominant", "70.0")),
    "live_runtimes": (False, {}, lambda w: setattr(w, "rachio_api", (
        {"id-front": 33.0}, {"id-front": 8.0}, {"id-front": 20.0}))),
    "level_4_emergency": (False, {}, lambda w: w.set(
        "select.geodrops_rachio_drought_level", "Level 4 - Emergency")),
}


def _world(freezer, data, tweak):
    freezer.move_to(T_PLAN)
    w = FakeWorld(freezer)
    populate(w, data)
    tweak(w)
    return w


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_plan_context_matches_legacy(freezer, name, caplog):
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)

    self_cal, files, tweak = SCENARIOS[name]
    data = entry_data(self_cal=self_cal)

    lw = _world(freezer, data, tweak)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    cfg = ns["config"].parse_config(build_config(data))
    prime(ns, cfg)
    legacy_ctx = ns["_plan_context"](cfg)
    ns["_publish_targets"](cfg)
    legacy_skip = ns["_rain_skip_check"](legacy_ctx)

    nw = _world(freezer, data, tweak)
    docs = {LEGACY_FILE_KEYS[f]: v for f, v in files.items()}
    eng = native_engine(nw, data, PlanningMixin, RunnerMixin, IOMixin, docs=docs)
    ncfg = eng._load_cfg()
    prime(eng, ncfg)
    native_ctx = await eng._plan_context(ncfg)
    await eng._publish_targets(ncfg)
    native_skip = eng._rain_skip_check(native_ctx)

    assert native_ctx == legacy_ctx
    assert native_skip == legacy_skip
    assert nw.calls == legacy_calls(lw)
    assert eng.records["targets"]["attributes"] == \
        lw.published["pyscript.geodrops_rachio_targets"][1]
    assert eng.store.read("efficacy") == lf.files.get("irrigation_efficacy.json")
    assert log_trail_native(caplog) == log_trail_legacy(lw)

    # Per-scenario evidence: pin that the legacy run actually reached the
    # named condition, so no scenario silently degrades to "normal".
    if name == "normal":
        # The baseline every other scenario is checked NOT to silently
        # collapse into: both zones triggered (dominant 60 < floor 65), the
        # overnight forecast sensors are present so the window cap comes from
        # the forecast (not the instant fallback), neither zone has a live
        # Rachio runtime so both fall back to the static config value, and
        # nothing is uncompleted (no exclusion/offline/drop).
        assert set(legacy_ctx["priority"]) == {"front", "back"}
        assert legacy_ctx["cap_source"] == "forecast"
        assert legacy_ctx["runtime_sources"] == {"front": "static", "back": "static"}
        assert legacy_ctx["uncompleted"] == {}
    elif name == "excluded":
        assert legacy_ctx["uncompleted"].get("back") == "excluded"
    elif name == "low_quality":
        assert legacy_ctx["uncompleted"].get("front") == "low_quality"
    elif name == "forecast_missing":
        assert legacy_ctx["forecast_wx"] is None
        assert legacy_ctx["cap_source"] == "instant"
    elif name == "one_zone_wet":
        assert "back" not in legacy_ctx["priority"]
        assert "front" in legacy_ctx["priority"]
    elif name == "probe_calibrating":
        # front is untriggered (dominant 70 >= floor 65) but calibrating with
        # headroom, so it is appended as a probe candidate; the probe override
        # must show up as its dosing source (not a deficit-based dose), and it
        # must be the last (lowest-priority) entry, per the probe-appending
        # comment in _plan_context. "back" (converged, not calibrating) must
        # not be probed.
        assert legacy_ctx["dosing_sources"].get("front") == "probe"
        assert legacy_ctx["priority"][-1] == "front"
        assert legacy_ctx["dosing_sources"].get("back") != "probe"
    elif name == "live_runtimes":
        assert legacy_ctx["runtime_sources"].get("front") == "live"
        assert legacy_ctx["api_runtimes"] == {"id-front": 33.0}
    elif name == "level_4_emergency":
        assert legacy_ctx["level"] == "Level 4 - Emergency"
        assert legacy_ctx["priority"] == []


async def test_plan_context_raises_on_missing_zone_sensor(freezer, caplog):
    """Legacy `state.get` RAISES NameError for a nonexistent entity, and
    `_read_zone_signals` (legacy lines 1183-1190) has no enclosing
    `except NameError:` — the exception propagates all the way out of
    `_plan_context` (into the nightly run's own abort handling), it does NOT
    quietly mark the zone "unavailable" while the other zone still waters.
    Native must match: `self._state_get` raises the same `NameError`, not
    `self.port.state` returning None. See "Controller ruling (after Task 8):
    missing entities" in global-constraints-and-port-rules.md.
    """
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)
    data = entry_data()
    tweak = lambda w: w.remove("sensor.back_dominant")  # noqa: E731

    lw = _world(freezer, data, tweak)
    ns = load_legacy(lw, build_config(data), LegacyFiles())
    cfg = ns["config"].parse_config(build_config(data))
    prime(ns, cfg)
    with pytest.raises(NameError) as legacy_exc:
        ns["_plan_context"](cfg)

    nw = _world(freezer, data, tweak)
    eng = native_engine(nw, data, PlanningMixin, RunnerMixin, IOMixin)
    ncfg = eng._load_cfg()
    prime(eng, ncfg)
    with pytest.raises(NameError) as native_exc:
        await eng._plan_context(ncfg)

    assert str(native_exc.value) == str(legacy_exc.value) == "sensor.back_dominant"
    # Nothing had been called/logged before the raise on either side (the
    # "front" zone processes cleanly first; "back" raises before any service
    # call, log, or store write happens for it).
    assert nw.calls == legacy_calls(lw) == []
    assert log_trail_native(caplog) == log_trail_legacy(lw) == []


async def test_is_rain_sustain_and_hail(freezer):
    data = entry_data()
    w = _world(freezer, data, lambda w: None)
    eng = native_engine(w, data, PlanningMixin, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    w.set("sensor.tempest_sensor_precipitation_type", "rain")
    w.set("sensor.tempest_rain_last_hour", "1.0")
    assert eng._is_rain() is False          # sustain clock starts
    assert eng._is_rain_at_start() is True  # no sustain at block start
    freezer.tick(151)
    assert eng._is_rain() is True
    w.set("sensor.tempest_sensor_precipitation_type", "hail")
    eng._rain_since = None
    assert eng._is_rain() is True           # hail is immediate
