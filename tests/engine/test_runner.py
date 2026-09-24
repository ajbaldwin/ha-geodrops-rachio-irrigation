"""Runner branches the scenario suites do not reach, asserted directly on the
engine's own effects (no golden fixture)."""
import logging

import pytest

from custom_components.geodrops_rachio.brain.plan import Slot
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from tests.engine.helpers import ENGINE_LOGGER_PREFIX, prime
from tests.engine.scenario import entry_data, native_engine, populate
from tests.engine.world import FakeWorld

T0 = "2026-07-02 02:00:00"
FRONT, BACK = "switch.front_zone", "switch.back_zone"
SWITCHES = {"front": FRONT, "back": BACK}
START = ("rachio", "start_multiple_zone_schedule")
STOP_DEVICE = ("rachio", "stop_watering", {"devices": "Main House"})
MARKER = "switch.geodrops_rachio_run_active"


def _engine(freezer, *, collapse, overrides=""):
    if not collapse:
        overrides = "use_pause_collapse: false\n" + overrides
    data = entry_data(overrides=overrides)
    freezer.move_to(T0)
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_engine(w, data, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    flags = {"standby": False, "stop": False, "rain": False, "rain_start": False}
    preds = (lambda: flags["standby"], lambda: flags["stop"],
             lambda: flags["rain"], lambda: flags["rain_start"])
    return w, eng, flags, preds


async def _run(eng, collapse, slots, preds):
    runner = eng.run_collapsed if collapse else eng.run_plan
    return await runner(slots, dict(SWITCHES), *preds)


def _starts(w):
    return [c for c in w.calls if c[:2] == START]


def _turn_offs(w, switches=(FRONT, BACK)):
    return [c[2]["entity_id"] for c in w.calls
            if c[:2] == ("switch", "turn_off") and c[2]["entity_id"] in switches]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name.startswith(ENGINE_LOGGER_PREFIX) and r.levelname == "WARNING"]


WATER = [Slot("front", 10), Slot("back", 10)]
WITH_SOAK = [Slot("front", 10), Slot(None, 30), Slot("back", 10)]
BOTH = pytest.mark.parametrize("collapse", [True, False], ids=["collapsed", "run_plan"])


@BOTH
async def test_standby_before_first_block_waters_nothing(freezer, collapse):
    w, eng, flags, preds = _engine(freezer, collapse=collapse)
    flags["standby"] = True
    out = await _run(eng, collapse, WATER, preds)
    assert out["aborted_reason"] == "standby"
    assert out["watered"] == [] and out["delivered_minutes"] == {}
    assert _starts(w) == []
    assert sorted(_turn_offs(w)) == [BACK, FRONT]            # stop_all, defensively
    if collapse:
        assert STOP_DEVICE in w.calls
        assert w.get(MARKER) == "off"                          # finally cleared it


@BOTH
async def test_rain_at_start_refuses_to_start_a_block(freezer, collapse):
    w, eng, flags, preds = _engine(freezer, collapse=collapse)
    flags["rain_start"] = True
    out = await _run(eng, collapse, WATER, preds)
    assert out["aborted_reason"] == "rain-at-start"
    assert _starts(w) == [] and out["watered"] == []


@BOTH
async def test_zone_already_running_is_stopped_before_the_handover(freezer, collapse, caplog):
    w, eng, _flags, preds = _engine(freezer, collapse=collapse)
    w.rachio.start_direct([(BACK, 30)])                        # an orphan, not ours
    out = await _run(eng, collapse, WATER, preds)
    assert out["aborted_reason"] is None
    first_start = w.calls.index(_starts(w)[0])
    assert ("switch", "turn_off", {"entity_id": BACK}) in w.calls[:first_start]
    assert any(f"{BACK} was already running before" in m for m in _warnings(caplog))


@BOTH
async def test_unexpected_error_still_closes_every_valve(freezer, collapse):
    w, eng, _flags, preds = _engine(freezer, collapse=collapse)
    w.failing.add(START)
    with pytest.raises(RuntimeError, match="start_multiple_zone_schedule failed"):
        await _run(eng, collapse, WATER, preds)
    failed_at = w.calls.index(next(c for c in w.calls if c[:2] == START))
    after = w.calls[failed_at + 1:]
    assert {c[2]["entity_id"] for c in after if c[:2] == ("switch", "turn_off")} >= {
        FRONT, BACK}
    if collapse:
        assert STOP_DEVICE in after
        assert w.get(MARKER) == "off"


async def test_run_plan_manual_stop_during_soak_keeps_earlier_delivery(freezer):
    w, eng, flags, preds = _engine(freezer, collapse=False)
    w.at("2026-07-02 02:20:00", lambda: flags.__setitem__("stop", True))  # mid-soak
    out = await _run(eng, False, WITH_SOAK, preds)
    assert out["aborted_reason"] == "manual-abort"
    assert out["watered"] == ["front"] and out["delivered_minutes"] == {"front": 10}
    assert len(_starts(w)) == 1


async def test_run_plan_skips_a_block_that_rounds_to_nothing(freezer):
    w, eng, _flags, preds = _engine(freezer, collapse=False)
    out = await _run(eng, False, [Slot("front", 0.2)], preds)
    assert out == {"watered": [], "aborted_reason": None,
                   "delivered_minutes": {}, "blocks": []}
    assert _starts(w) == []


async def test_run_plan_stops_a_zone_still_running_after_its_block(freezer, caplog):
    w, eng, _flags, preds = _engine(freezer, collapse=False)
    # Something re-queues front for 20 min a moment after our 10 min block starts,
    # so the block's end never drains.
    w.at("2026-07-02 02:00:01", lambda: w.rachio.start_direct([(FRONT, 20)]))
    out = await _run(eng, False, [Slot("front", 10)], preds)
    assert out["aborted_reason"] is None
    assert any("still running 120s after its block" in m for m in _warnings(caplog))
    assert _turn_offs(w)[-2:] == [FRONT, BACK]


async def test_collapsed_empty_plan_touches_nothing(freezer):
    w, eng, _flags, preds = _engine(freezer, collapse=True)
    out = await _run(eng, True, [], preds)
    assert out == {"watered": [], "aborted_reason": None, "delivered_minutes": {},
                   "blocks": [], "recoveries": 0, "breadcrumbs": []}
    assert w.calls == []


async def test_collapsed_long_soak_chains_pauses(freezer):
    w, eng, _flags, preds = _engine(freezer, collapse=True)
    slots = [Slot("front", 10), Slot(None, 90), Slot("back", 10)]
    out = await _run(eng, True, slots, preds)
    assert out["aborted_reason"] is None
    assert out["delivered_minutes"] == {"front": 10, "back": 10}
    pauses = [c[2]["duration"] for c in w.calls if c[:2] == ("rachio", "pause_watering")]
    assert pauses == [60, 30]
    assert len(_starts(w)) == 1                                # one schedule all night
    events = [c["event"] for c in out["breadcrumbs"]]
    assert events == ["start", "pause", "resume", "pause", "resume", "end"]


async def test_collapsed_resume_that_does_not_take_hold_gives_up(freezer, caplog):
    """No retries left: a schedule that resumes to nothing ends the run."""
    w, eng, _flags, preds = _engine(freezer, collapse=True,
                                     overrides="max_schedule_retries: 0")
    # Rachio drops the schedule while paused: the resume brings nothing back.
    w.at("2026-07-02 02:20:00", w.rachio._clear)
    out = await _run(eng, True, WITH_SOAK, preds)
    assert out["aborted_reason"] == "never-started"
    assert out["delivered_minutes"] == {"front": 10}
    assert out["recoveries"] == 0
    events = [c["event"] for c in out["breadcrumbs"]]
    assert events[-2:] == ["drop-detected", "give-up"]
    assert w.calls[-1][:2] == ("switch", "turn_off")           # stop_all last



async def test_collapsed_manual_stop_mid_pause(freezer):
    w, eng, flags, preds = _engine(freezer, collapse=True)
    w.at("2026-07-02 02:20:00", lambda: flags.__setitem__("stop", True))  # paused
    out = await _run(eng, True, WITH_SOAK, preds)
    assert out["aborted_reason"] == "manual-abort"
    assert out["delivered_minutes"] == {"front": 10}
    assert ("rachio", "resume_watering") not in [c[:2] for c in w.calls]
    assert STOP_DEVICE in w.calls and w.get(MARKER) == "off"

BOUNDED = "max_pauses_per_schedule: 1"
TWO_SOAKS = [Slot("front", 5), Slot(None, 20), Slot("back", 5), Slot(None, 20),
             Slot("front", 5)]


async def test_collapsed_bounded_budget_idles_between_segments(freezer):
    w, eng, _flags, preds = _engine(freezer, collapse=True, overrides=BOUNDED)
    out = await _run(eng, True, TWO_SOAKS, preds)
    assert out["aborted_reason"] is None
    assert len(_starts(w)) == 2                                # budget forced a split
    assert out["delivered_minutes"] == {"front": 10, "back": 5}


async def test_collapsed_abort_during_segment_gap(freezer):
    w, eng, flags, preds = _engine(freezer, collapse=True, overrides=BOUNDED)
    # Segment 1 (front, pause 20, back) ends ~02:30; the 20 min gap runs to ~02:50.
    w.at("2026-07-02 02:40:00", lambda: flags.__setitem__("rain", True))
    out = await _run(eng, True, TWO_SOAKS, preds)
    assert out["aborted_reason"] == "rain-abort"
    assert len(_starts(w)) == 1
    assert out["delivered_minutes"] == {"front": 5, "back": 5}
    assert STOP_DEVICE in w.calls


async def test_collapsed_standby_before_second_segment(freezer):
    w, eng, flags, preds = _engine(freezer, collapse=True, overrides=BOUNDED)
    # Raised after the gap's last watch poll, so the segment loop's own check
    # (not the gap watch) is what catches it.
    w.at("2026-07-02 02:49:59", lambda: flags.__setitem__("standby", True))
    out = await _run(eng, True, TWO_SOAKS, preds)
    assert out["aborted_reason"] == "standby"
    assert len(_starts(w)) == 1


@pytest.fixture(autouse=True)
def _warnings_captured(caplog):
    caplog.set_level(logging.WARNING, logger=ENGINE_LOGGER_PREFIX)


# --- late external stop vs Rachio drop (collapsed) ------------------------
# WITH_SOAK collapses to: water front 10 (02:00-02:10), pause 30, water back 10
# (02:40-02:50). A stop in the last ~270 s of a step never reaches
# EXTERNAL_STOP_POLLS before the end grace, so the step itself ends "cleanly".

async def test_late_stop_before_a_pause_is_an_external_stop_not_a_drop(freezer):
    w, eng, _flags, preds = _engine(freezer, collapse=True)
    w.at("2026-07-02 02:07:00", w.rachio._clear)       # 180 s before front ends
    out = await _run(eng, True, WITH_SOAK, preds)
    assert out["aborted_reason"] == "external-stop"
    assert out["recoveries"] == 0
    assert len(_starts(w)) == 1                         # not re-issued
    # Credited to the first empty poll (02:07:00, where the water stopped), not
    # the full 10 minutes.
    assert out["delivered_minutes"] == {"front": 7.0}


async def test_drop_during_a_pause_is_still_recovered(freezer):
    w, eng, _flags, preds = _engine(freezer, collapse=True)
    w.rachio.drop_at = w.now().replace(minute=20)      # 02:20, mid-pause
    out = await _run(eng, True, WITH_SOAK, preds)
    assert out["aborted_reason"] is None
    assert out["recoveries"] == 1
    assert len(_starts(w)) == 2
    assert out["delivered_minutes"] == {"front": 10, "back": 10}


async def test_late_stop_in_the_last_step_still_ends_the_night_cleanly(freezer):
    # Nothing follows the last water step, so the stop evidence is never used:
    # it can only ever veto a re-issue, never end a night on its own.
    w, eng, _flags, preds = _engine(freezer, collapse=True)
    w.at("2026-07-02 02:47:00", w.rachio._clear)       # 180 s before back ends
    out = await _run(eng, True, WITH_SOAK, preds)
    assert out["aborted_reason"] is None
    assert out["recoveries"] == 0 and len(_starts(w)) == 1
