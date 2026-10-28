import pytest

from brain import calibration
from brain.config import Tunables


def test_efficacy_to_span():
    assert calibration.efficacy_to_span(0.5, 40.0) == 20.0


def test_first_probe_uses_fraction_or_floor():
    t = Tunables()  # fraction 1/3, floor 10
    assert calibration.probe_minutes(24.0, None, None, t) == 10.0            # 1/3*24=8 -> floor 10
    assert calibration.probe_minutes(71.0, None, None, t) == pytest.approx(71 / 3)   # 1/3*71≈23.7
    assert calibration.probe_minutes(100.0, None, None, t) == pytest.approx(100 / 3)


def test_probe_grows_on_small_rise():
    t = Tunables()  # growth 1.5, measurable 3.0
    assert calibration.probe_minutes(40.0, 10.0, 1.0, t) == 15.0      # rise<3 -> grow
    assert calibration.probe_minutes(40.0, 10.0, 5.0, t) == 10.0      # rise>=3 -> hold


def test_saturation_cap():
    t = Tunables()  # saturation_reject 95
    assert calibration.cap_for_saturation(20.0, 90.0, 0.5, t) == 10.0  # room 5 / eff .5 = 10
    assert calibration.cap_for_saturation(20.0, 50.0, None, t) == 20.0  # unknown eff, unchanged


def obs(pre=60, mins=30, settled=72, training=False, rained=False, ok=True):
    return calibration.Observation("z", pre, mins, settled, training, rained, ok)


def test_classify_priority():
    t = Tunables()
    assert calibration.classify(obs(ok=False), t) == "unavailable"
    assert calibration.classify(obs(training=True), t) == "training"
    assert calibration.classify(obs(rained=True), t) == "rain"
    assert calibration.classify(obs(settled=96), t) == "saturated"
    assert calibration.classify(obs(settled=60), t) == "no_rise"
    assert calibration.classify(obs(), t) == "ok"


def test_classify_a_rise_below_measurable_is_too_small_to_learn():
    t = Tunables()  # measurable_rise_pts 3.0
    assert calibration.classify(obs(settled=62), t) == "small_rise"     # +2: noise band
    assert calibration.classify(obs(settled=62.9), t) == "small_rise"
    assert calibration.classify(obs(settled=63), t) == "ok"             # +3: measurable
    assert calibration.classify(obs(settled=59), t) == "no_rise"        # <=0 keeps its name


def test_update_and_converged():
    t = Tunables()
    e = calibration.update_efficacy(None, obs(60, 30, 72), t)  # 12/30 = 0.4
    assert e == 0.4
    assert calibration.converged([0.40, 0.42, 0.38], t) is True
    assert calibration.converged([0.40, 0.50, 0.38], t) is False
    assert calibration.converged([0.40, 0.42], t) is False


def test_converged_short_dose_uses_the_points_floor():
    t = Tunables()  # tolerance 10%, floor 2.0 pts
    # 13-min doses: spread 0.10/min = 1.3 pts of rise, inside the 2-pt probe noise
    # floor though 23% of the mean.
    assert calibration.converged([0.377, 0.477, 0.42], t, minutes=13) is True
    assert calibration.converged([0.377, 0.477, 0.42], t) is False   # no dose: 10% only
    # 0.20/min over 13 min = 2.6 pts: past the floor too.
    assert calibration.converged([0.30, 0.50, 0.40], t, minutes=13) is False


def test_converged_long_dose_keeps_the_relative_band():
    t = Tunables()
    # 28-min doses: spread 0.11/min = 3.1 pts, past both the floor and 10%.
    assert calibration.converged([0.614, 0.504, 0.56], t, minutes=28) is False
    # A big rate on a long dose: 10% of the mean (0.2/min) is wider than the floor
    # (2/40 = 0.05/min), so the floor never narrows the band.
    assert calibration.converged([1.9, 2.08, 2.0], t, minutes=40) is True


def test_consistent_count_is_the_newest_samples_that_agree():
    t = Tunables()  # floor 2.0 pts
    assert calibration.consistent_count([], t, 28) == 0
    assert calibration.consistent_count([0.6], t, 28) == 1
    # 28-min doses: 0.614 and 0.657 agree (1.2 pts); 0.504 is 4.3 pts off the newest.
    assert calibration.consistent_count([0.614, 0.504, 0.657], t, 28) == 1
    assert calibration.consistent_count([0.504, 0.614, 0.657], t, 28) == 2
    # 13-min doses, 1.3 pts apart: both agree.
    assert calibration.consistent_count([0.377, 0.477], t, 13) == 2


def test_a_full_consistent_window_is_exactly_convergence():
    t = Tunables()
    for recent, minutes in [([0.377, 0.477, 0.42], 13), ([0.614, 0.504, 0.56], 28),
                            ([1.9, 2.08, 2.0], 40)]:
        full = calibration.consistent_count(recent, t, minutes) == len(recent)
        assert full is calibration.converged(recent, t, minutes)


def test_off_rate_uses_the_same_band():
    t = Tunables()
    assert calibration.off_rate(0.477, 0.377, 13, t) is False   # 1.3 pts: noise
    assert calibration.off_rate(0.504, 0.614, 28, t) is True    # 3.1 pts: a real miss
    assert calibration.off_rate(0.4, 0.1, 20, t) is True        # 6 pts
    assert calibration.off_rate(2.15, 2.0, 40, t) is False      # within 10%
    assert calibration.off_rate(0.5, None, 20, t) is False      # nothing learned yet
    assert calibration.off_rate(0.5, 0.0, 20, t) is False


def test_converged_efficacy_is_the_mean_of_recent():
    assert calibration.converged_efficacy([0.40, 0.42, 0.38]) == pytest.approx(0.40)


def test_next_state():
    t = Tunables()  # convergence_samples=3
    assert calibration.next_state("calibrating", False, True, 0, t) == "recalibrating"   # training wins
    assert calibration.next_state("converged", True, True, 0, t) == "recalibrating"       # training wins from converged too
    assert calibration.next_state("calibrating", True, False, 0, t) == "converged"
    assert calibration.next_state("recalibrating", True, False, 0, t) == "converged"
    assert calibration.next_state("converged", False, False, 3, t) == "calibrating"        # miss streak
    assert calibration.next_state("converged", False, False, 1, t) == "converged"          # streak too low
    assert calibration.next_state("calibrating", False, False, 0, t) == "calibrating"      # unchanged


def test_should_probe():
    t = Tunables()  # probe_headroom_ceiling=85
    assert calibration.should_probe("calibrating", 70.0, False, t) is True
    assert calibration.should_probe("recalibrating", 70.0, False, t) is True
    assert calibration.should_probe("converged", 70.0, False, t) is False   # not calibrating
    assert calibration.should_probe("calibrating", 70.0, True, t) is False  # pinned
    assert calibration.should_probe("calibrating", 90.0, False, t) is False # no headroom
    assert calibration.should_probe("unresponsive", 70.0, False, t) is False  # repair open


import datetime as dt


def _t(h):
    return dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=h)


# --- settle_decision (accumulate model) ---
def test_settle_decision_accumulates_before_finalize():
    # run_end=_t(0), retain_hours=6 -> finalize at _t(6)
    assert calibration.settle_decision(_t(3), _t(0), 6.0, 12.0, True) == "accumulate"
    assert calibration.settle_decision(_t(3), _t(0), 6.0, 12.0, False) == "accumulate"

def test_settle_decision_finalizes_when_ripe_with_retained():
    assert calibration.settle_decision(_t(6), _t(0), 6.0, 12.0, True, True) == "finalize"   # boundary
    assert calibration.settle_decision(_t(9), _t(0), 6.0, 12.0, True, True) == "finalize"

def test_settle_decision_waits_for_retained_then_finalizes_peak_only():
    # ripe with a peak but no retained reading yet: a reading taken after the
    # settle floor may still be uploading, so keep collecting through the grace
    assert calibration.settle_decision(_t(6), _t(0), 6.0, 12.0, True) == "accumulate"
    assert calibration.settle_decision(_t(17), _t(0), 6.0, 12.0, True) == "accumulate"
    # grace over: finalize on the peak alone (retention left unchanged)
    assert calibration.settle_decision(_t(18), _t(0), 6.0, 12.0, True) == "finalize"   # boundary

def test_settle_decision_waits_past_finalize_without_sample_then_expires():
    # past finalize (_t6) but within grace (+12h -> _t18): still accumulate, hoping for a sample
    assert calibration.settle_decision(_t(10), _t(0), 6.0, 12.0, False) == "accumulate"
    # at/after finalize+max_wait (_t18) with no sample ever -> expired
    assert calibration.settle_decision(_t(18), _t(0), 6.0, 12.0, False) == "expired"   # boundary
    assert calibration.settle_decision(_t(20), _t(0), 6.0, 12.0, False) == "expired"

def test_settle_decision_sample_finalizes_even_past_grace():
    assert calibration.settle_decision(_t(20), _t(0), 6.0, 12.0, True) == "finalize"
    assert calibration.settle_decision(_t(20), _t(0), 6.0, 12.0, True, True) == "finalize"

# --- accumulate_sample (gated on when the reading was TAKEN) ---
def test_accumulate_first_sample_sets_peak_not_retained_before_settle():
    # read at _t(1) < run_end+settle(4) -> retained stays None, peak set
    peak, ret, seen, ch = calibration.accumulate_sample(
        None, None, None, 85.0, _t(1), _t(0), 4.0)
    assert (peak, ret, ch) == (85.0, None, True) and seen == _t(1)

def test_accumulate_running_max_and_retained_after_settle():
    # second sample lower value, read after settle -> peak holds, retained updates
    peak, ret, seen, ch = calibration.accumulate_sample(
        85.0, None, _t(1), 82.0, _t(5), _t(0), 4.0)
    assert (peak, ret, ch) == (85.0, 82.0, True) and seen == _t(5)
    # a higher later sample raises peak and updates retained
    peak, ret, seen, ch = calibration.accumulate_sample(
        85.0, 82.0, _t(5), 88.0, _t(6), _t(0), 4.0)
    assert (peak, ret, seen, ch) == (88.0, 88.0, _t(6), True)

def test_accumulate_retained_keys_off_reading_time_not_arrival():
    # a reading TAKEN before the settle floor is peak-only, however late it
    # arrives (batched upload + pipeline lag): it shows soil near run_end
    peak, ret, seen, ch = calibration.accumulate_sample(
        85.0, None, _t(1), 84.0, _t(3), _t(0), 4.0)
    assert (peak, ret, seen, ch) == (85.0, None, _t(3), True)

def test_accumulate_ignores_stale_republish_rewrite_and_missing():
    # read_at not newer than last_seen -> no change (stale republish, or the
    # GeoDrops reprocess rewriting the value with no new reading)
    assert calibration.accumulate_sample(85.0, 82.0, _t(5), 99.0, _t(5), _t(0), 4.0) == (85.0, 82.0, _t(5), False)
    # None value/read_at -> no change
    assert calibration.accumulate_sample(85.0, 82.0, _t(5), None, _t(6), _t(0), 4.0) == (85.0, 82.0, _t(5), False)
    assert calibration.accumulate_sample(85.0, 82.0, _t(5), 90.0, None, _t(0), 4.0) == (85.0, 82.0, _t(5), False)

def test_accumulate_ignores_reading_taken_before_run_end():
    # the latest reading was taken before run_end (even if it reached HA after
    # it, in a batched upload) -> not a sample, peak stays unset
    assert calibration.accumulate_sample(None, None, None, 70.0, _t(-1), _t(0), 4.0) == (None, None, None, False)
    # a reading taken exactly at run_end counts ("from run_end on")
    peak, ret, seen, ch = calibration.accumulate_sample(
        None, None, None, 85.0, _t(0), _t(0), 4.0)
    assert (peak, ret, seen, ch) == (85.0, None, _t(0), True)

# --- upgrade_pre (the pre-watering baseline, gated on when it was TAKEN) ---
def test_upgrade_pre_takes_a_newer_reading_taken_before_the_valve_opened():
    # a reading taken after the snapshot's but before the zone opened arrives
    # late (GeoDrops lag) -> it is the closer baseline
    assert calibration.upgrade_pre(60.0, _t(-3), 58.0, _t(-1), _t(0)) == (58.0, _t(-1))

def test_upgrade_pre_ignores_readings_taken_once_the_valve_opened():
    # taken during watering (or after) -> not a baseline, however it arrives
    assert calibration.upgrade_pre(60.0, _t(-3), 62.0, _t(0), _t(0)) == (60.0, _t(-3))
    assert calibration.upgrade_pre(60.0, _t(-3), 62.0, _t(1), _t(0)) == (60.0, _t(-3))

def test_upgrade_pre_ignores_stale_and_missing():
    # not newer than the baseline it would replace -> no change
    assert calibration.upgrade_pre(60.0, _t(-1), 58.0, _t(-1), _t(0)) == (60.0, _t(-1))
    assert calibration.upgrade_pre(60.0, _t(-1), 58.0, _t(-2), _t(0)) == (60.0, _t(-1))
    assert calibration.upgrade_pre(60.0, _t(-3), None, _t(-1), _t(0)) == (60.0, _t(-3))
    assert calibration.upgrade_pre(60.0, _t(-3), 58.0, None, _t(0)) == (60.0, _t(-3))

def test_upgrade_pre_needs_the_valve_open_time():
    # an obs queued before valve-open times were recorded keeps its baseline
    assert calibration.upgrade_pre(60.0, None, 58.0, _t(-1), None) == (60.0, None)

def test_upgrade_pre_with_no_snapshot_time_takes_any_pre_open_reading():
    assert calibration.upgrade_pre(60.0, None, 58.0, _t(-1), _t(0)) == (58.0, _t(-1))

# --- retention_factor ---
def test_retention_factor_and_clamps():
    assert calibration.retention_factor(80.0, 92.0, 84.0, 0.1) == pytest.approx((84-80)/(92-80))  # 0.333
    assert calibration.retention_factor(80.0, 92.0, 120.0, 0.1) == 1.0        # clamp high
    assert calibration.retention_factor(80.0, 92.0, 80.0, 0.1) == 0.1         # fully drained -> floor
    assert calibration.retention_factor(80.0, 80.0, 84.0, 0.1) is None        # peak<=pre -> None
    assert calibration.retention_factor(80.0, 92.0, None, 0.1) is None        # no retained -> None

# --- retained_span ---
def test_retained_span_applies_r_and_clamps():
    assert calibration.retained_span(0.5, 0.4, 60.0, 1.0, 60.0) == pytest.approx(12.0)   # .5*.4*60
    assert calibration.retained_span(0.5, None, 60.0, 1.0, 60.0) == pytest.approx(30.0)  # r None -> 1.0
    assert calibration.retained_span(2.0, 1.0, 60.0, 1.0, 60.0) == 60.0                  # clamp high
    assert calibration.retained_span(0.001, 0.1, 60.0, 1.0, 60.0) == 1.0                 # clamp low

# --- ewma ---
def test_ewma():
    assert calibration.ewma(None, 0.4, 0.3) == 0.4               # bootstrap
    assert calibration.ewma(0.5, 0.4, 0.3) == pytest.approx(0.3*0.4 + 0.7*0.5)


def test_exclusion_return_resets_after_threshold():
    now = dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5, "span_pts": 20.0,
           "recent": [0.5, 0.5, 0.5], "miss_streak": 0, "n_obs": 5,
           "excluded_since": dt.datetime(2026, 9, 17, 12, 0, 0).isoformat()}  # 72h
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "recalibrating"
    assert out["efficacy"] is None
    assert out["span_pts"] == 0
    assert out["recent"] == []
    assert "excluded_since" not in out
    assert out["n_obs"] == 5  # history count preserved


def test_exclusion_return_keeps_below_threshold():
    now = dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5, "span_pts": 20.0,
           "excluded_since": dt.datetime(2026, 9, 20, 0, 0, 0).isoformat()}  # 12h
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "converged"
    assert out["efficacy"] == 0.5
    assert out["span_pts"] == 20.0
    assert "excluded_since" not in out  # stamp cleared


def test_exclusion_return_no_stamp_is_noop():
    now = dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5}
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out == {"state": "converged", "efficacy": 0.5}


def test_exclusion_return_malformed_stamp_clears_keeps():
    now = dt.datetime(2026, 9, 20, 12, 0, 0)
    rec = {"state": "converged", "efficacy": 0.5, "excluded_since": "not-a-date"}
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "converged"
    assert out["efficacy"] == 0.5
    assert "excluded_since" not in out


def test_apply_reject_no_rise_records_growth_fields():
    t = Tunables()  # growth 1.5, measurable 3.0, floor 10
    out = calibration.apply_reject({"state": "calibrating"}, "no_rise", 10.0, -0.5, t)
    assert out["last_reject_reason"] == "no_rise"
    assert out["prior_minutes"] == 10.0
    assert out["last_rise"] == -0.5
    # the recorded fields must let the next probe grow, not restart at the floor
    assert calibration.probe_minutes(40.0, out["prior_minutes"], out["last_rise"], t) == 15.0


def test_apply_reject_small_rise_grows_the_probe_like_no_rise():
    t = Tunables()  # growth 1.5, measurable 3.0
    out = calibration.apply_reject({"state": "calibrating"}, "small_rise", 10.0, 1.3, t)
    assert out["last_reject_reason"] == "small_rise"
    assert (out["prior_minutes"], out["last_rise"]) == (10.0, 1.3)
    assert calibration.probe_minutes(40.0, out["prior_minutes"], out["last_rise"], t) == 15.0


def test_unresponsive_needs_a_full_refill_that_still_did_not_rise():
    # a full refill is the most one night delivers: no measurable rise from it
    # means the zone is not reaching the probe, whatever the probe size
    for reason in ("no_rise", "small_rise"):
        assert calibration.unresponsive(reason, 50.0, 50.3) is True    # Rachio rounds down
        assert calibration.unresponsive(reason, 49.3, 50.3) is True    # within 1 min
        assert calibration.unresponsive(reason, 49.2, 50.3) is False   # still room to grow
    # every other reason says nothing about the zone's response
    for reason in ("ok", "saturated", "rain", "unavailable", "training"):
        assert calibration.unresponsive(reason, 50.0, 50.0) is False
    # an obs queued before the full refill was recorded never decides it
    assert calibration.unresponsive("no_rise", 50.0, None) is False
    assert calibration.unresponsive("no_rise", 50.0, 0.0) is False


def test_resume_restarts_an_unresponsive_zone_from_a_first_probe():
    rec = {"state": "unresponsive", "efficacy": 0.4, "span_pts": 8.0,
           "recent": [0.4], "n_obs": 3, "prior_minutes": 50.0, "last_rise": 0.5,
           "miss_streak": 1, "last_reject_reason": "small_rise",
           "unresponsive_since": "2026-07-02T11:30:00+00:00", "retention": 0.6}
    out = calibration.resume(rec)
    assert out == {"state": "recalibrating", "efficacy": None, "span_pts": 0,
                   "recent": [], "n_obs": 3, "prior_minutes": None,
                   "last_rise": None, "miss_streak": 0,
                   "last_reject_reason": None, "retention": 0.6}
    # restarts at the first-probe size, not the stuck full refill
    assert calibration.probe_minutes(60.0, out["prior_minutes"], out["last_rise"],
                                     Tunables()) == 20.0
    assert rec["state"] == "unresponsive"                  # input untouched


def test_small_rise_miss_needs_a_rate_that_predicted_a_measurable_rise():
    t = Tunables()  # measurable 3.0
    # 0.5 pts/min x 10 min predicts +5: an unmeasurable rise is a miss
    assert calibration.small_rise_miss(0.5, 10.0, t) is True
    assert calibration.small_rise_miss(0.3, 10.0, t) is True       # predicts exactly 3
    # a small dose was expected to give a small rise: not a miss
    assert calibration.small_rise_miss(0.2, 10.0, t) is False
    assert calibration.small_rise_miss(None, 10.0, t) is False
    assert calibration.small_rise_miss(0.0, 10.0, t) is False


def test_exclusion_return_restarts_an_unresponsive_zone_from_a_first_probe():
    import datetime as _dt
    now = _dt.datetime(2026, 7, 5, 12, 0)
    rec = {"state": "unresponsive", "efficacy": 0.4, "span_pts": 8.0,
           "recent": [0.4], "prior_minutes": 50.0, "last_rise": 0.5,
           "unresponsive_since": "2026-07-01T12:00:00",
           "excluded_since": "2026-07-01T12:00:00"}
    out = calibration.exclusion_return(rec, now, 48.0)
    assert out["state"] == "recalibrating"
    assert out["prior_minutes"] is None and out["last_rise"] is None
    assert "unresponsive_since" not in out and "excluded_since" not in out
    # a short exclusion keeps the zone unresponsive (its repair stays open)
    short = calibration.exclusion_return(
        {**rec, "excluded_since": "2026-07-05T00:00:00"}, now, 48.0)
    assert short["state"] == "unresponsive"


def test_apply_reject_saturated_shrinks_and_holds():
    t = Tunables()  # shrink 1.5
    out = calibration.apply_reject({"state": "calibrating"}, "saturated", 30.0, 12.0, t)
    assert out["last_reject_reason"] == "saturated"
    assert out["prior_minutes"] == pytest.approx(30.0 / t.probe_shrink)  # 20.0
    assert out["last_rise"] is None
    # next probe holds at the shrunk size (does not grow or restart at floor)
    nxt = calibration.probe_minutes(71.0, out["prior_minutes"], out["last_rise"], t)
    assert nxt == pytest.approx(30.0 / t.probe_shrink)


def test_apply_reject_confounded_reasons_do_not_resize():
    for reason in ("rain", "unavailable", "training"):
        out = calibration.apply_reject({"state": "calibrating"}, reason, 10.0, 2.0, t=Tunables())
        assert out["last_reject_reason"] == reason
        assert "prior_minutes" not in out
        assert "last_rise" not in out


def test_apply_reject_does_not_mutate_input():
    rec = {"state": "calibrating"}
    calibration.apply_reject(rec, "no_rise", 10.0, -0.5, Tunables())
    assert rec == {"state": "calibrating"}
