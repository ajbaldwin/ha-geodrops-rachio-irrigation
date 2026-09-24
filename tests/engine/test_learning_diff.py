import logging

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.diff import (
    ENGINE_LOGGER_PREFIX, assert_same_effects, freeze, legacy_effects, log_trail_legacy, log_trail_native,
)
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import ALL_MIXINS, entry_data, native_engine, populate
from tests.engine import golden
from tests.engine.world import FakeWorld

RUN_END = "2026-07-02T04:30:00+00:00"


def _obs(zone):
    return {"zone": zone, "pre_dominant": 60.0, "minutes": 20, "run_end_iso": RUN_END,
            "peak": None, "retained": None, "last_seen_updated": None}


SETTLE = {
    "accept": ({"sensor.front_dominant": [("2026-07-02 05:00:00", "68.0"),
                                          ("2026-07-02 07:00:00", "66.0")]}, {}),
    "no_rise_reject": ({"sensor.front_dominant": [("2026-07-02 05:00:00", "59.0")]}, {}),
    # The dominant reading must settle (peak captured) BEFORE the zone's
    # quality sensors flip to "Training" — a zone reading Training is itself
    # offline (sensors.read_zone: Training is not a usable quality), so a
    # scenario where all three read Training from the start would never
    # capture a sample at all (it would expire, not reject-as-training).
    "training": ({"sensor.front_dominant": [("2026-07-02 05:00:00", "68.0")],
                 "sensor.front_q1": [("2026-07-02 05:30:00", "Training")],
                 "sensor.front_q2": [("2026-07-02 05:30:00", "Training")],
                 "sensor.front_q3": [("2026-07-02 05:30:00", "Training")]}, {}),
    # Dominant sensor unavailable for the whole window: never a genuine
    # reading, so peak never accumulates and the obs runs out the clock
    # (settle_max_wait_hours past finalize) without ever finalizing.
    "expired_no_fresh_reading": ({}, {"sensor.front_dominant": "unavailable"}),
}


def _world(freezer, data, readings, statics):
    freezer.move_to("2026-07-02 04:30:00")
    w = FakeWorld(freezer)
    populate(w, data)
    for ent, val in statics.items():
        w.set(ent, val)
    for ent, series in readings.items():
        for when, val in series:
            w.at(when, lambda e=ent, v=val: w.set(e, v))
    return w


def _front_rec(lf):
    return lf.files["irrigation_efficacy.json"]["front"]


def _assert_settle_branch(name, lf):
    """Confirm the legacy side actually reached the outcome the scenario is
    named for — equivalence alone would not catch a scenario that silently
    landed on the wrong calibration branch (see task-10-brief.md Step 4)."""
    front = _front_rec(lf)
    pending = lf.files["irrigation_pending_obs.json"]
    if name == "accept":
        assert front["n_obs"] == 1
        assert front["last_reject_reason"] is None
        assert pending == []
    elif name == "no_rise_reject":
        assert front["last_reject_reason"] == "no_rise"
        assert front.get("n_obs") is None
        assert pending == []
    elif name == "training":
        assert front["last_reject_reason"] == "training"
        assert front["state"] == "recalibrating"
        assert front["efficacy"] is None
        assert pending == []
    elif name == "expired_no_fresh_reading":
        assert pending == []
        assert front == {"state": "calibrating"}  # untouched: no model change
    else:  # pragma: no cover - every scenario must be pinned
        raise AssertionError(f"no branch evidence for {name}")


@pytest.mark.parametrize("name", list(SETTLE))
async def test_settle_matches_legacy(freezer, name, caplog):
    # INFO, not WARNING: _settle_and_learn logs a summary at info level, and
    # the log-trail comparison below must see it on both sides too.
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER_PREFIX)
    readings, statics = SETTLE[name]
    data = entry_data(self_cal=True)
    files = {"irrigation_pending_obs.json": [_obs("front")],
             "irrigation_efficacy.json": {"front": {"state": "calibrating"}}}

    lw = _world(freezer, data, readings, statics)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    for _ in range(80):                 # 40 h of 30-min polls
        lw.advance(1800)
        ns["_settle_and_learn"]()

    nw = _world(freezer, data, readings, statics)
    eng = native_engine(nw, data, *ALL_MIXINS,
                        docs={LEGACY_FILE_KEYS[f]: v for f, v in files.items()})
    for _ in range(80):
        for coro in nw.advance(1800):
            await coro
        await eng._settle_and_learn()

    assert_same_effects(lw, lf, nw, eng)
    assert log_trail_native(caplog) == log_trail_legacy(lw)
    freeze(f"settle/{name}", legacy_effects(lw, lf),
           golden.engine_effects(nw, eng, log_trail_native(caplog)))
    _assert_settle_branch(name, lf)


@pytest.mark.parametrize("has_nightly", [True, False])
async def test_calibrate_matches_legacy(freezer, has_nightly, caplog):
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)
    data = entry_data()
    nightly_attrs = {"pressure_forecast": {"warm": False, "humid": True,
                                           "stagnant": False, "count": 1}}

    freezer.move_to("2026-07-02 06:00:00")
    lw = FakeWorld(freezer)
    populate(lw, data)
    lf = LegacyFiles()
    ns = load_legacy(lw, build_config(data), lf)
    if has_nightly:
        lw.publish("pyscript.geodrops_rachio_last_nightly", 2, nightly_attrs)
    ns["irrigation_calibrate"]()

    freezer.move_to("2026-07-02 06:00:00")
    nw = FakeWorld(freezer)
    populate(nw, data)
    eng = native_engine(nw, data, *ALL_MIXINS)
    if has_nightly:
        eng._publish("last_nightly", 2, nightly_attrs)
    await eng.irrigation_calibrate()

    assert_same_effects(lw, lf, nw, eng)
    assert log_trail_native(caplog) == log_trail_legacy(lw)
    freeze("calibrate/" + ("with_nightly" if has_nightly else "no_nightly"), legacy_effects(lw, lf),
           golden.engine_effects(nw, eng, log_trail_native(caplog)))
    # Step 4: has_nightly True vs False must visibly differ in the published
    # calibration record (present only when a nightly forecast was available).
    cal = lw.published.get("pyscript.geodrops_rachio_calibration")
    if has_nightly:
        assert cal is not None
    else:
        assert cal is None
