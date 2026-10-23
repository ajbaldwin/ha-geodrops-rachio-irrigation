"""A zone whose GeoDrops probe has its Active switch off is excluded.

GeoDrops versions before the switch have none, and the switch can be missing,
disabled or unavailable; only an explicit "off" takes the zone out.
"""
import datetime as dt

import pytest

from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from custom_components.geodrops_rachio.engine.store import EFFICACY
from tests.engine.helpers import prime
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

ACTIVE = "switch.back_probe_active"


def _engine(freezer, active_state, *, link=True, self_cal=False, docs=None):
    freezer.move_to(T_PLAN)
    data = entry_data(self_cal=self_cal)
    world = FakeWorld(freezer)
    populate(world, data)
    if link:
        world.active_links["sensor.back_dominant"] = ACTIVE
    if active_state is not None:
        world.set(ACTIVE, active_state)
    eng = native_engine(world, data, PlanningMixin, RunnerMixin, IOMixin, docs=docs)
    cfg = eng._load_cfg()
    prime(eng, cfg)
    return world, eng, cfg


async def test_an_inactive_probe_takes_its_zone_out_of_the_plan(freezer):
    _world, eng, cfg = _engine(freezer, "off")
    ctx = await eng._plan_context(cfg)
    assert ctx["uncompleted"].get("back") == "probe_inactive"
    assert "back" not in ctx["priority"]
    assert "front" in ctx["priority"]
    # Stamped like an exclusion, so a long absence recalibrates on return.
    assert "excluded_since" in eng.store.read(EFFICACY)["back"]


@pytest.mark.parametrize(("state", "link"), [
    ("on", True),
    ("unavailable", True),   # GeoDrops not loaded, or downgraded (orphaned)
    ("unknown", True),
    (None, True),            # switch entity disabled: no state
    (None, False),           # GeoDrops before the switch, or not a GeoDrops sensor
])
async def test_anything_but_off_leaves_the_zone_planned(freezer, state, link):
    _world, eng, cfg = _engine(freezer, state, link=link)
    ctx = await eng._plan_context(cfg)
    assert set(ctx["priority"]) == {"front", "back"}
    assert ctx["uncompleted"] == {}


async def test_the_exclude_switch_is_reported_ahead_of_an_inactive_probe(freezer):
    world, eng, cfg = _engine(freezer, "off")
    world.set("switch.geodrops_rachio_back_exclude", "on")
    ctx = await eng._plan_context(cfg)
    assert ctx["uncompleted"].get("back") == "excluded"


async def test_a_dry_plan_does_not_stamp_an_inactive_probe(freezer):
    _world, eng, cfg = _engine(freezer, "off")
    ctx = await eng._plan_context(cfg, dry=True)
    assert ctx["uncompleted"].get("back") == "probe_inactive"
    assert "back" not in (eng.store.read(EFFICACY) or {})


async def test_an_inactive_calibrating_probe_is_not_watched_for_recovery(freezer):
    docs = {EFFICACY: {"back": {"state": "calibrating"}}}
    _world, eng, cfg = _engine(freezer, "off", self_cal=True, docs=docs)
    ctx = await eng._plan_context(cfg)
    assert ctx["uncompleted"].get("back") == "probe_inactive"
    assert "back" not in ctx["recovery_candidates"]


async def test_a_probe_back_after_a_long_absence_recalibrates(freezer):
    docs = {EFFICACY: {"back": {"state": "converged", "efficacy": 0.5,
                                "span_pts": 12.0}}}
    world, eng, cfg = _engine(freezer, "off", self_cal=True, docs=docs)
    await eng._plan_context(cfg)

    freezer.tick(dt.timedelta(hours=49))
    world.set(ACTIVE, "on")
    ctx = await eng._plan_context(cfg)
    assert "back" in ctx["dominant_by_zone"]
    rec = eng.store.read(EFFICACY)["back"]
    assert rec["state"] == "recalibrating"
    assert "excluded_since" not in rec
