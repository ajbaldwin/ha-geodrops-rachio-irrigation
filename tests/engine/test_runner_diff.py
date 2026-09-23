import copy
import datetime as dt

import pytest

from custom_components.geodrops_rachio.brain import plan as brain_plan
from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from tests.engine.diff import legacy_calls, prime
from tests.engine.legacy_harness import load_legacy
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


def _setup_world(freezer, data, tweak):
    freezer.move_to(T0)
    world = FakeWorld(freezer)
    populate(world, data)
    flags = {"stop": False, "rain": False}
    tweak(world, flags)
    preds = (lambda: False, lambda: flags["stop"], lambda: flags["rain"], lambda: False)
    return world, flags, preds


SCENARIOS = {
    "normal": lambda w, f: None,
    "rachio_drop_recovers": lambda w, f: setattr(
        w.rachio, "drop_at", w.now() + dt.timedelta(minutes=20)),
    "never_started": lambda w, f: setattr(w.rachio, "refuse_next_start", True),
    "external_stop": lambda w, f: w.at("2026-07-02 02:10:00", w.rachio._clear),
    "manual_stop": lambda w, f: w.at("2026-07-02 02:10:00",
                                     lambda: f.__setitem__("stop", True)),
    "rain_abort": lambda w, f: w.at("2026-07-02 02:10:00",
                                    lambda: f.__setitem__("rain", True)),
}


@pytest.mark.parametrize("collapse", [True, False], ids=["collapsed", "run_plan"])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_runner_matches_legacy(freezer, name, collapse):
    overrides = "" if collapse else "use_pause_collapse: false"
    data = entry_data(overrides=overrides)
    runner = "run_collapsed" if collapse else "run_plan"

    lw, _lf, lpreds = _setup_world(freezer, data, SCENARIOS[name])
    ns = load_legacy(lw, build_config(data))
    cfg = ns["config"].parse_config(build_config(data))
    prime(ns, cfg)
    legacy_out = ns[runner](_slots(cfg), dict(SWITCHES), *lpreds)

    nw, _nf, npreds = _setup_world(freezer, data, SCENARIOS[name])
    eng = native_engine(nw, data, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    native_out = await getattr(eng, runner)(_slots(eng._current_cfg), dict(SWITCHES), *npreds)

    assert native_out == legacy_out
    assert nw.calls == legacy_calls(lw)
    assert (eng.api_calls, eng.state_polls) == (ns["api_calls"], ns["state_polls"])
