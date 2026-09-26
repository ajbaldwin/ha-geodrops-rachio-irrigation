"""Learning branches the scenario suites do not reach, asserted directly on the
engine's own effects (no golden fixture)."""
import asyncio
import logging

import pytest

from custom_components.geodrops_rachio.engine.store import EFFICACY, PENDING_OBS
from tests.engine.helpers import ENGINE_LOGGER_PREFIX
from tests.engine.scenario import (ALL_MIXINS, entry_data, native_engine,
                                   native_scheduler, populate)
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


async def test_unparseable_last_seen_stamp_counts_as_never_seen(freezer):
    obs = {**_obs("front"), "last_seen_updated": "garbage"}
    w, eng = _engine(freezer, pending=[obs])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    await _poll(w, eng, 14)
    front = eng.store.read(EFFICACY)["front"]
    assert front["n_obs"] == 1 and front["last_rise"] == pytest.approx(8.0)


async def test_a_zone_that_cannot_be_read_keeps_its_obs_for_the_next_poll(freezer, caplog):
    w, eng = _engine(freezer, pending=[_obs("front")])
    w.remove("sensor.front_q1")            # a renamed/deleted quality sensor
    await _poll(w, eng, 1)
    assert eng.store.read(PENDING_OBS) == [_obs("front")]
    assert ("warning", "irrigation: settle-and-learn skipped a record (sensor.front_q1)") in [
        (r.levelname.lower(), r.getMessage()) for r in caplog.records]


async def test_obs_finalizing_together_fetch_runtimes_once(freezer):
    w, eng = _engine(freezer, pending=[_obs("front"), _obs("back")])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.back_dominant", "66.0"))
    fetches = []
    real = eng.get_runtimes

    async def counting():
        fetches.append(1)
        return await real()
    eng.get_runtimes = counting
    await _poll(w, eng, 14)
    eff = eng.store.read(EFFICACY)
    assert eff["front"]["n_obs"] == 1 and eff["back"]["n_obs"] == 1
    assert len(fetches) == 1


def _during_runtime_fetch(eng, coro_fn):
    """Run `coro_fn()` as its own task while the settle poll is awaiting its
    runtime fetch (the one await between reading and rewriting pending_obs);
    returns the list the task is recorded in."""
    tasks = []
    real = eng.get_runtimes

    async def fetch_with_concurrent_writer():
        tasks.append(asyncio.ensure_future(coro_fn()))
        for _ in range(5):
            await asyncio.sleep(0)
        return await real()
    eng.get_runtimes = fetch_with_concurrent_writer
    return tasks


async def test_an_obs_appended_during_the_settle_poll_is_kept(freezer):
    """A run that finishes while the poll is fetching runtimes appends its obs;
    the poll's rewrite of the queue must not erase it."""
    w, eng = _engine(freezer, pending=[_obs("front")])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    late = _obs("back", run_end="2026-07-02T10:30:00+00:00")
    tasks = _during_runtime_fetch(eng, lambda: eng._append_pending_obs([late]))
    await _poll(w, eng, 14)
    await asyncio.gather(*tasks)
    assert tasks and eng.store.read(PENDING_OBS) == [late]


async def test_an_obs_dropped_during_the_settle_poll_stays_dropped(freezer):
    """A Rachio run recorded while the poll is fetching drops the zone's sample
    (it would credit the wrong watering); the poll must not write it back."""
    w, eng = _engine(freezer, pending=[
        _obs("front"), _obs("back", run_end="2026-07-02T10:00:00+00:00")])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    tasks = _during_runtime_fetch(eng, lambda: eng._drop_pending_obs(["back"]))
    await _poll(w, eng, 14)
    await asyncio.gather(*tasks)
    assert tasks and eng.store.read(PENDING_OBS) == []


@pytest.mark.parametrize("field, value", [("pre_dominant", None), ("minutes", 0)])
async def test_obs_without_a_pre_reading_or_minutes_is_dropped_unlearned(
        freezer, field, value):
    w, eng = _engine(freezer, pending=[{**_obs("front"), field: value}])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    await _poll(w, eng, 14)
    assert eng.store.read(PENDING_OBS) == []
    assert eng.store.read(EFFICACY) == {"front": {"state": "calibrating"}}


async def test_accept_trims_recent_and_counts_a_miss(freezer):
    w, eng = _engine(freezer, pending=[_obs("front")])
    await eng.store.write(EFFICACY, {"front": {
        "state": "calibrating", "efficacy": 0.1, "recent": [0.1, 0.2, 0.3],
        "miss_streak": 0}})
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    await _poll(w, eng, 14)
    front = eng.store.read(EFFICACY)["front"]
    # (68-60)/20 = 0.4 per minute: appended, oldest dropped (convergence_samples=3).
    assert front["recent"] == pytest.approx([0.2, 0.3, 0.4])
    # |0.4 - 0.1| / 0.1 is far past convergence_tolerance (0.10): a miss.
    assert front["miss_streak"] == 1


async def test_a_corrupt_zone_record_skips_that_obs(freezer, caplog):
    """A finalize failure (here a corrupt stored zone record) is logged and the
    obs dropped; it is not retried, since the next poll would fail the same way."""
    w, eng = _engine(freezer, pending=[_obs("front")])
    await eng.store.write(EFFICACY, {"front": "corrupt"})
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    await _poll(w, eng, 14)
    assert eng.store.read(PENDING_OBS) == []
    assert eng.store.read(EFFICACY) == {"front": "corrupt"}
    assert any(r.getMessage().startswith("irrigation: settle-and-learn skipped a record (")
               and r.levelname == "WARNING" for r in caplog.records)


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
    # The span the observed sensors actually average (weather_derive).
    assert cal["window"] == "23:00-06:00"
    assert any("forecast 0/3 vs observed 0/3 — all signals agree" in m
               for m in _logbook(w))


async def test_calibrate_names_each_mismatch(freezer):
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 1, {"pressure_forecast": {
        "warm": False, "humid": True, "stagnant": False, "count": 1}})
    await eng.irrigation_calibrate()
    assert eng.records["calibration"]["attributes"]["mismatches"] == ["humid"]
    assert any("— humid forecast-high" in m for m in _logbook(w))


HUMID_FORECAST = {"warm": False, "humid": True, "stagnant": False, "count": 1}


def _run_holding_until(eng, gate, attrs):
    """A stand-in nightly that is still in progress (waiting or watering) at
    06:00 and publishes its last_nightly record only when `gate` opens."""
    async def fake_plan_and_run(wait, trigger):
        eng._run_in_progress = True
        try:
            await gate.wait()
            eng._publish("last_nightly", 0, attrs)
        finally:
            eng._run_in_progress = False
    eng._plan_and_run = fake_plan_and_run


async def test_calibrate_waits_for_a_run_in_progress_then_compares(freezer, caplog):
    """From late September the watering window closes after 06:00, so the nightly
    is still in progress when calibration fires. Calibration must wait for it
    (its record is the forecast side) rather than skip every morning."""
    freezer.move_to("2026-07-02 06:00:00")
    data = entry_data()
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    gate = asyncio.Event()
    _run_holding_until(eng, gate, {"updated": "2026-07-01T23:00:00",
                                   "pressure_forecast": HUMID_FORECAST})
    await eng.irrigation_nightly()
    await asyncio.sleep(0)
    cal = asyncio.ensure_future(eng.irrigation_calibrate())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not cal.done() and "calibration" not in eng.records
    # The observed side is the 06:00 snapshot, not a later reading.
    w.set("sensor.observed_overnight_humidity", "97")
    gate.set()
    await cal
    rec = eng.records["calibration"]["attributes"]
    assert rec["mismatches"] == ["humid"] and rec["observed_rh_pct"] == 80.0
    assert not any("calibration skipped" in r.getMessage() for r in caplog.records)


async def test_calibrate_leaves_the_run_scoped_config_alone(freezer):
    """Calibration reads its own config; it must not overwrite the globals a
    live run reads through (the reason it used to refuse to run at all)."""
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 0, {"pressure_forecast": HUMID_FORECAST})
    sentinel_cfg, sentinel_bindings = object(), object()
    eng._current_cfg, eng._current_bindings = sentinel_cfg, sentinel_bindings
    await eng.irrigation_calibrate()
    assert "calibration" in eng.records
    assert eng._current_cfg is sentinel_cfg
    assert eng._current_bindings is sentinel_bindings


async def test_calibrate_ignores_a_stale_nightly_record(freezer):
    """A night with no nightly record of its own (HA down, a reset) must not be
    scored against an older night's forecast."""
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 0, {"updated": "2026-06-29T23:00:00",
                                     "pressure_forecast": HUMID_FORECAST})
    await eng.irrigation_calibrate()
    assert "calibration" not in eng.records
    assert any("no nightly run to compare against" in m for m in _logbook(w))


async def test_calibrate_skips_without_observed_means(freezer, caplog):
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 0, {"pressure_forecast": {"count": 0}})
    w.remove("sensor.observed_overnight_humidity")
    await eng.irrigation_calibrate()
    assert "calibration" not in eng.records
    assert any("overnight observed means unavailable" in r.getMessage()
               for r in caplog.records)


async def test_calibrate_without_a_run_task_does_not_wait(freezer):
    """An engine with the flag set but no run task (nothing to wait on) scores
    the night rather than hanging."""
    w, eng = _calibrate_engine(freezer)
    eng._run_in_progress = True
    eng._publish("last_nightly", 0, {"pressure_forecast": HUMID_FORECAST})
    await eng.irrigation_calibrate()
    assert eng.records["calibration"]["attributes"]["mismatches"] == ["humid"]


@pytest.mark.parametrize("stamp, scored", [
    ("2026-07-01T23:00:00+00:00", True),     # aware, 7 h old
    ("2026-07-01T17:59:00+00:00", False),    # aware, just past 12 h
    ("not a time", False),
])
async def test_calibrate_nightly_stamp_freshness(freezer, stamp, scored):
    w, eng = _calibrate_engine(freezer)
    eng._publish("last_nightly", 0, {"updated": stamp,
                                     "pressure_forecast": HUMID_FORECAST})
    await eng.irrigation_calibrate()
    assert ("calibration" in eng.records) is scored


async def test_all_training_depths_reject_the_sample_in_either_format(freezer):
    w, eng = _engine(freezer, pending=[_obs("front")])
    w.at("2026-07-02 05:00:00", lambda: w.set("sensor.front_dominant", "68.0"))
    for q in ("sensor.front_q1", "sensor.front_q2", "sensor.front_q3"):
        w.at("2026-07-02 10:00:00", lambda q=q: w.set(q, "training"))
    await _poll(w, eng, 14)
    front = eng.store.read(EFFICACY)["front"]
    assert front["last_reject_reason"] == "training" and front["efficacy"] is None
