"""A calibrating zone too wet for its probe (at or above the probe headroom
ceiling) is reported as "Too Wet", at plan time and at the window-start
re-check, instead of disappearing from the recap without a word.
"""
import random

import pytest

from custom_components.geodrops_rachio.engine.store import EFFICACY
from tests.engine.helpers import prime
from tests.engine.scenario import ALL_MIXINS, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

_Random = random.Random

CAL = {EFFICACY: {"front": {"state": "calibrating"},
                  "back": {"state": "recalibrating"}}}


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


def _engine(freezer, *, self_cal=True, docs=CAL, back="86.0"):
    freezer.move_to("2026-07-01 22:59:58")
    data = entry_data(self_cal=self_cal)
    world = FakeWorld(freezer)
    populate(world, data)
    world.set("sensor.back_dominant", back)
    eng = native_engine(world, data, *ALL_MIXINS, docs=docs)
    return world, eng


async def _plan(eng):
    cfg = eng._load_cfg()
    prime(eng, cfg)
    return await eng._plan_context(cfg)


def _recap(world):
    return [d["message"] for dom, svc, d in world.calls
            if (dom, svc) == ("notify", "phone")][-1]


async def test_a_calibrating_zone_above_the_ceiling_is_too_wet(freezer):
    _world, eng = _engine(freezer)
    ctx = await _plan(eng)
    assert ctx["uncompleted"].get("back") == "too_wet"
    assert "back" not in ctx["priority"]


async def test_at_the_ceiling_is_too_wet(freezer):
    _world, eng = _engine(freezer, back="85.0")
    ctx = await _plan(eng)
    assert ctx["uncompleted"].get("back") == "too_wet"


async def test_below_the_ceiling_the_zone_is_probed(freezer):
    _world, eng = _engine(freezer, back="84.9")
    ctx = await _plan(eng)
    assert "back" in ctx["priority"]
    assert "back" not in ctx["uncompleted"]


@pytest.mark.parametrize(("self_cal", "docs"), [
    (True, {EFFICACY: {"back": {"state": "converged", "efficacy": 0.5}}}),
    (False, CAL),   # calibration off: no zone probes, so none is "too wet"
])
async def test_a_zone_that_would_not_probe_anyway_is_not_flagged(
        freezer, self_cal, docs):
    _world, eng = _engine(freezer, self_cal=self_cal, docs=docs)
    ctx = await _plan(eng)
    assert "back" not in ctx["uncompleted"]


async def test_the_recap_says_too_wet(freezer):
    world, eng = _engine(freezer)
    for coro in world.advance(2):
        await coro
    await eng._plan_and_run(True, "nightly")
    assert "Not completed: Back (Too Wet)" in _recap(world)


async def test_a_probe_too_wet_by_window_start_is_too_wet(freezer):
    world, eng = _engine(freezer, back="60.0")
    world.at("2026-07-02 01:00:00", lambda: world.set("sensor.back_dominant", "86.0"))
    for coro in world.advance(2):
        await coro
    await eng._plan_and_run(True, "nightly")
    attrs = eng.records["last_run"]["attributes"]
    assert attrs["uncompleted"].get("back") == "too_wet"
    assert "Back (Too Wet)" in _recap(world)
