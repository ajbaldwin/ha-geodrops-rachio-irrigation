import logging
import random

import pytest

from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from custom_components.geodrops_rachio.engine.store import EFFICACY
from tests.engine import golden
from tests.engine.helpers import ENGINE_LOGGER_PREFIX, log_trail_native, prime
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

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
    "probe_calibrating": (True, {EFFICACY: EFFICACY_CAL},
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
async def test_plan_context_scenario(freezer, name, caplog):
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)

    self_cal, files, tweak = SCENARIOS[name]
    data = entry_data(self_cal=self_cal)

    world = _world(freezer, data, tweak)
    eng = native_engine(world, data, PlanningMixin, RunnerMixin, IOMixin, docs=files)
    cfg = eng._load_cfg()
    prime(eng, cfg)
    ctx = await eng._plan_context(cfg)
    await eng._publish_targets(cfg)
    skip = eng._rain_skip_check(ctx)

    golden.check(f"planning/{name}", {
        "ctx": ctx, "skip": skip, "calls": [list(c) for c in world.calls],
        "targets": eng.records["targets"]["attributes"],
        "efficacy": eng.store.read("efficacy"),
        "logs": [list(e) for e in log_trail_native(caplog)]})

    # Per-scenario evidence: pin that the run actually reached the named
    # condition, so no scenario silently degrades to "normal" (and a
    # regenerated fixture cannot quietly hide that it did).
    if name == "normal":
        # The baseline every other scenario is checked NOT to silently
        # collapse into: both zones triggered (dominant 60 < floor 65), the
        # overnight forecast sensors are present so the window cap comes from
        # the forecast (not the instant fallback), neither zone has a live
        # Rachio runtime so both fall back to the static config value, and
        # nothing is uncompleted (no exclusion/offline/drop).
        assert set(ctx["priority"]) == {"front", "back"}
        assert ctx["cap_source"] == "forecast"
        assert ctx["runtime_sources"] == {"front": "static", "back": "static"}
        assert ctx["uncompleted"] == {}
    elif name == "excluded":
        assert ctx["uncompleted"].get("back") == "excluded"
    elif name == "low_quality":
        assert ctx["uncompleted"].get("front") == "low_quality"
    elif name == "forecast_missing":
        assert ctx["forecast_wx"] is None
        assert ctx["cap_source"] == "instant"
    elif name == "one_zone_wet":
        assert "back" not in ctx["priority"]
        assert "front" in ctx["priority"]
    elif name == "probe_calibrating":
        # front is untriggered (dominant 70 >= floor 65) but calibrating with
        # headroom, so it is appended as a probe candidate; the probe override
        # must show up as its dosing source (not a deficit-based dose), and it
        # must be the last (lowest-priority) entry, per the probe-appending
        # comment in _plan_context. "back" (converged, not calibrating) must
        # not be probed.
        assert ctx["dosing_sources"].get("front") == "probe"
        assert ctx["priority"][-1] == "front"
        assert ctx["dosing_sources"].get("back") != "probe"
    elif name == "live_runtimes":
        assert ctx["runtime_sources"].get("front") == "live"
        assert ctx["api_runtimes"] == {"id-front": 33.0}
    elif name == "level_4_emergency":
        assert ctx["level"] == "Level 4 - Emergency"
        assert ctx["priority"] == []


async def test_plan_context_raises_on_missing_zone_sensor(freezer, caplog):
    """`state.get` RAISES NameError for a nonexistent entity, and the zone-signal
    read has no enclosing `except NameError:` -- the exception propagates all
    the way out of `_plan_context` (into the nightly run's own abort handling);
    it does NOT quietly mark the zone "unavailable" while the other zone still
    waters. v0.9.15 behaved the same; `self._state_get` raises, rather than
    `self.port.state` returning None.
    """
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)
    data = entry_data()
    tweak = lambda w: w.remove("sensor.back_dominant")  # noqa: E731

    world = _world(freezer, data, tweak)
    eng = native_engine(world, data, PlanningMixin, RunnerMixin, IOMixin)
    cfg = eng._load_cfg()
    prime(eng, cfg)
    with pytest.raises(NameError) as exc:
        await eng._plan_context(cfg)

    assert str(exc.value) == "sensor.back_dominant"
    # Nothing is called or logged before the raise (the "front" zone processes
    # cleanly first; "back" raises before any service call, log, or store write).
    assert world.calls == []
    assert log_trail_native(caplog) == []


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
