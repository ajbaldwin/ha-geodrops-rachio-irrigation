import datetime as dt
import logging

import pytest

from custom_components.geodrops_rachio.brain import plan as brain_plan
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from tests.engine import golden
from tests.engine.helpers import ENGINE_LOGGER_PREFIX, log_trail_native, prime
from tests.engine.scenario import entry_data, native_engine, populate
from tests.engine.world import FakeWorld

T0 = "2026-07-02 02:00:00"
SWITCHES = {"front": "switch.front_zone", "back": "switch.back_zone"}


def _slots(cfg):
    tun = cfg.tunables
    p = brain_plan.build_plan(
        ["front", "back"], {"front": 30.0, "back": 24.0},
        {"front": "front", "back": "back"}, {"front": (), "back": ()}, 360.0, tun)
    return p.slots


def _setup_world(freezer, data, tweak, collapse):
    freezer.move_to(T0)
    world = FakeWorld(freezer)
    populate(world, data)
    flags = {"stop": False, "rain": False}
    tweak(world, flags, collapse)
    preds = (lambda: False, lambda: flags["stop"], lambda: flags["rain"], lambda: False)
    return world, flags, preds


def _drop_at(w, f, collapse):
    """Schedule a Rachio-side schedule drop.

    The collapsed program here is: water front 12 (02:00), water back 12
    (02:12), pause 8 (02:24-02:32), water front 12, water back 12, pause 8,
    water front 6. A real drop happens WHILE PAUSED (Rachio's cumulative-pause
    limit): water runs right up to the pause and the schedule is gone at the
    resume. So for run_collapsed the drop lands mid-pause (02:26:40); the
    post-resume probe finds nothing running, the step reports "never-started"
    with prior delivery > 0 and no stop seen before the pause -> RECOVER.

    run_plan issues each block itself and never pauses, so a drop can only
    land inside a block, where it is indistinguishable from an external stop:
    at 15 min (900s) the margin to the 1440s block end is 540s, >= 270s (6
    misses * 30s + the 90s grace), so the block reports "external-stop" --
    run_plan has no retry/recovery path, so this is the only branch a drop can
    reach there.
    """
    offset = (dt.timedelta(minutes=26, seconds=40) if collapse
              else dt.timedelta(minutes=15))
    w.rachio.drop_at = w.now() + offset


def _late_stop(w, f, collapse):
    """Something outside the scheduler stops the watering at 02:20 -- 240s before
    the back step (run_collapsed) or the front+back block (run_plan) ends, too
    late for EXTERNAL_STOP_POLLS to accumulate before the end grace. run_collapsed
    sees the valves off for good at the step end, so the next step's non-start is
    the stop, not a drop: no re-issue. run_plan (the non-default fallback) credits
    the block and issues the next one itself, as it always has."""
    w.at("2026-07-02 02:20:00", w.rachio._clear)


# Times chosen so every scripted stop/rain/manual event lands >= 270s before
# the end of the step or block it is scripted to interrupt (6 misses *
# CHECK_INTERVAL_S=30 + BLOCK_END_GRACE_S=90), for BOTH runners' step/block
# shapes, so each poll-count-based verdict (external-stop) actually fires
# instead of being silently absorbed by the end grace -- except
# late_external_stop, which lands inside that window on purpose (the stop the
# grace absorbs, which must still not be re-issued). manual_stop/rain_abort
# are unaffected by this: `_abort_now` is checked before the watch_switches
# poll-count logic on every tick, so those fire immediately regardless of
# block/step position.
SCENARIOS = {
    "normal": lambda w, f, collapse: None,
    "rachio_drop_recovers": _drop_at,
    "late_external_stop": _late_stop,
    "never_started": lambda w, f, collapse: setattr(w.rachio, "refuse_next_start", True),
    "external_stop": lambda w, f, collapse: w.at("2026-07-02 02:05:00", w.rachio._clear),
    "manual_stop": lambda w, f, collapse: w.at(
        "2026-07-02 02:10:00", lambda: f.__setitem__("stop", True)),
    "rain_abort": lambda w, f, collapse: w.at(
        "2026-07-02 02:10:00", lambda: f.__setitem__("rain", True)),
}

# Expected-outcome table: the aborted_reason each (scenario, runner) must reach,
# pinning that the scenario actually lands on its named branch rather than
# silently degrading to another one (e.g. "normal") -- otherwise a timing change
# could make two scenarios collapse onto the same result and a regenerated
# fixture would hide it. collapse=True -> run_collapsed, collapse=False ->
# run_plan. run_plan has no recovery path, so a drop it cannot absorb into a
# full recovery is the same observable failure as an external stop.
EXPECTED = {
    ("normal", True): {"aborted_reason": None},
    ("normal", False): {"aborted_reason": None},
    ("rachio_drop_recovers", True): {"aborted_reason": None, "recoveries_at_least": 1},
    ("rachio_drop_recovers", False): {"aborted_reason": "external-stop"},
    ("late_external_stop", True): {"aborted_reason": "external-stop", "recoveries": 0},
    ("late_external_stop", False): {"aborted_reason": None, "recoveries": 0},
    ("never_started", True): {"aborted_reason": "never-started"},
    ("never_started", False): {"aborted_reason": "never-started"},
    ("external_stop", True): {"aborted_reason": "external-stop"},
    ("external_stop", False): {"aborted_reason": "external-stop"},
    ("manual_stop", True): {"aborted_reason": "manual-abort"},
    ("manual_stop", False): {"aborted_reason": "manual-abort"},
    ("rain_abort", True): {"aborted_reason": "rain-abort"},
    ("rain_abort", False): {"aborted_reason": "rain-abort"},
}


def _run_effects(eng, world, caplog) -> dict:
    return {"calls": [list(c) for c in world.calls],
            "counters": [eng.api_calls, eng.state_polls],
            "logs": [list(e) for e in log_trail_native(caplog)]}


@pytest.mark.parametrize("collapse", [True, False], ids=["collapsed", "run_plan"])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_runner_scenario(freezer, name, collapse, caplog):
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)

    overrides = "" if collapse else "use_pause_collapse: false"
    data = entry_data(overrides=overrides)
    runner = "run_collapsed" if collapse else "run_plan"

    world, _flags, preds = _setup_world(freezer, data, SCENARIOS[name], collapse)
    eng = native_engine(world, data, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    out = await getattr(eng, runner)(_slots(eng._current_cfg), dict(SWITCHES), *preds)

    expected = EXPECTED[(name, collapse)]
    assert out["aborted_reason"] == expected["aborted_reason"], (name, collapse)
    if "recoveries_at_least" in expected:
        assert out["recoveries"] >= expected["recoveries_at_least"], (name, collapse)
    if "recoveries" in expected:
        assert out.get("recoveries", 0) == expected["recoveries"], (name, collapse)
    golden.check(f"runner/{name}-{runner}",
                 {"out": out, **_run_effects(eng, world, caplog)})


@pytest.mark.parametrize("collapse", [True, False], ids=["collapsed", "run_plan"])
async def test_missing_zone_switch_raises(freezer, collapse, caplog):
    """A zone switch entity that does not exist (e.g. a renamed/deleted Rachio
    zone switch) raises NameError straight out of the poll-verify loop that runs
    before every block/segment, through `run_plan`/`run_collapsed`'s own
    `except Exception: stop_all(...); raise` -- as v0.9.15's `poll_zone_running`
    did (it had no `except NameError:` guard). `poll_zone_running` uses
    `self._state_get`, not `self.port.state`, which would silently read "off"
    and water the other zones as if nothing were wrong.

    "front"'s switch is the normal, populated entity; "back" is pointed at
    "switch.ghost_zone", an entity nothing in this test ever registers, so it
    genuinely does not exist in the sim. The stop_all/stop_device cleanup that
    runs before the re-raise is pinned by the fixture.
    """
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)
    overrides = "" if collapse else "use_pause_collapse: false"
    data = entry_data(overrides=overrides)
    runner = "run_collapsed" if collapse else "run_plan"
    switches = {"front": "switch.front_zone", "back": "switch.ghost_zone"}

    world, _flags, preds = _setup_world(freezer, data, SCENARIOS["normal"], collapse)
    eng = native_engine(world, data, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    with pytest.raises(NameError) as exc:
        await getattr(eng, runner)(_slots(eng._current_cfg), dict(switches), *preds)

    assert str(exc.value) == "switch.ghost_zone"
    golden.check(f"runner/missing_zone_switch-{runner}", _run_effects(eng, world, caplog))
