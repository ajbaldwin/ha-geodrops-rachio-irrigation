"""Learning branches the scenario suites do not reach, asserted directly on the
engine's own effects (no golden fixture)."""
import logging

import pytest

from custom_components.geodrops_rachio.engine.store import EFFICACY, PENDING_OBS
from tests.engine.helpers import ENGINE_LOGGER_PREFIX
from tests.engine.scenario import ALL_MIXINS, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

RUN_END = "2026-07-02T04:30:00+00:00"


def _obs(zone, run_end=RUN_END):
    return {"zone": zone, "pre_dominant": 60.0, "minutes": 20, "run_end_iso": run_end,
            "peak": None, "retained": None, "last_seen_updated": None}


def _engine(freezer, *, pending, when="2026-07-02 04:30:00", self_cal=True):
    data = entry_data(self_cal=self_cal)
    freezer.move_to(when)
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_engine(w, data, *ALL_MIXINS, docs={
        PENDING_OBS: pending, EFFICACY: {"front": {"state": "calibrating"}}})
    return w, eng


async def _poll(w, eng, polls):
    for _ in range(polls):                 # 30-min settle polls
        for coro in w.advance(1800):
            await coro
        await eng._settle_and_learn()


def _logbook(w):
    return [d["message"] for dom, svc, d in w.calls if (dom, svc) == ("logbook", "log")]


@pytest.fixture(autouse=True)
def _logs(caplog):
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER_PREFIX)


# --- settle-and-learn -----------------------------------------------------

async def test_accept_with_a_retained_reading_learns_retention(freezer):
    w, eng = _engine(freezer, pending=[_obs("front")])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))  # peak
    # From run_end + settle_hours (08:30) on, readings count as "retained".
    w.at("2026-07-02 09:00:00", lambda: w.set("sensor.front_dominant", "64.0"))
    await _poll(w, eng, 14)                # to 11:30, past finalize (10:30)
    front = eng.store.read(EFFICACY)["front"]
    assert front["n_obs"] == 1 and front["last_reject_reason"] is None
    assert front["retention"] == pytest.approx(0.5)   # (64-60)/(68-60)
    assert front["last_rise"] == pytest.approx(8.0)
    assert eng.store.read(PENDING_OBS) == []


async def test_accept_without_a_retained_reading_keeps_retention_unset(freezer):
    w, eng = _engine(freezer, pending=[_obs("front")])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    await _poll(w, eng, 14)
    front = eng.store.read(EFFICACY)["front"]
    assert front["n_obs"] == 1 and front["retention"] is None


async def test_obs_for_an_unconfigured_zone_or_bad_timestamp_is_dropped(freezer):
    w, eng = _engine(freezer, pending=[_obs("gone"), _obs("front", run_end="not-a-time")])
    await _poll(w, eng, 1)
    assert eng.store.read(PENDING_OBS) == []
    assert eng.store.read(EFFICACY) == {"front": {"state": "calibrating"}}


async def test_settle_is_a_noop_when_self_calibration_is_off(freezer):
    w, eng = _engine(freezer, pending=[_obs("front")], self_cal=False)
    await _poll(w, eng, 1)
    assert eng.store.read(PENDING_OBS) == [_obs("front")]


async def test_settle_skips_the_poll_when_config_fails_to_load(freezer, caplog):
    w, eng = _engine(freezer, pending=[_obs("front")])

    def broken():
        raise ValueError("bad overrides")
    eng._load_raw_config = broken
    await _poll(w, eng, 1)
    assert eng.store.read(PENDING_OBS) == [_obs("front")]
    assert any("settle-and-learn skipped; config load failed" in r.getMessage()
               for r in caplog.records)


# --- forecast calibration -------------------------------------------------

def _calibrate_engine(freezer):
    freezer.move_to("2026-07-02 06:00:00")
    data = entry_data()
    w = FakeWorld(freezer)
    populate(w, data)       # observed overnight: 60F, 80% RH, 3 mph -> no pressure
    return w, native_engine(w, data, *ALL_MIXINS)


async def test_calibrate_reports_agreement(freezer):
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 0, {"pressure_forecast": {
        "warm": False, "humid": False, "stagnant": False, "count": 0}})
    await eng.irrigation_calibrate()
    cal = eng.records["calibration"]["attributes"]
    assert cal["mismatches"] == []
    assert any("forecast 0/3 vs observed 0/3 — all signals agree" in m
               for m in _logbook(w))


async def test_calibrate_names_each_mismatch(freezer):
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 1, {"pressure_forecast": {
        "warm": False, "humid": True, "stagnant": False, "count": 1}})
    await eng.irrigation_calibrate()
    assert eng.records["calibration"]["attributes"]["mismatches"] == ["humid"]
    assert any("— humid forecast-high" in m for m in _logbook(w))


async def test_calibrate_waits_for_a_run_in_progress(freezer, caplog):
    w, eng = _calibrate_engine(freezer)
    eng._run_in_progress = True
    await eng.irrigation_calibrate()
    assert "calibration" not in eng.records and _logbook(w) == []
    assert any("calibration skipped — an irrigation run is in progress" in r.getMessage()
               for r in caplog.records)


async def test_calibrate_skips_without_observed_means(freezer, caplog):
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 0, {"pressure_forecast": {"count": 0}})
    w.remove("sensor.observed_overnight_humidity")
    await eng.irrigation_calibrate()
    assert "calibration" not in eng.records
    assert any("overnight observed means unavailable" in r.getMessage()
               for r in caplog.records)
