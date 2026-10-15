"""brain/frost.py: where a cold morning moves the finish."""
import datetime as dt

import pytest

from custom_components.geodrops_rachio.brain.config import Tunables
from custom_components.geodrops_rachio.brain.frost import (
    FrostDecision, frost_shift, parse_hourly)

TZ = dt.timezone(dt.timedelta(hours=-4))
TUN = Tunables()  # frost_temp_f 38, frost_max_delay_minutes 90


def at(hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(2026, 11, 2, h, m, tzinfo=TZ)


SUNRISE = at("07:30")
BASE_END = at("07:30")  # sunrise anchor, offset 0 -> latest finish 09:00


def cold_until(hhmm: str | None) -> list[tuple[dt.datetime, float]]:
    """00:00-11:00 hourly: 30 °F before `hhmm`, 45 °F from it."""
    clear = at(hhmm) if hhmm else None
    return [(at(f"{h:02d}:00"), 30.0 if clear is None or at(f"{h:02d}:00") < clear
             else 45.0) for h in range(12)]


def test_no_cold_hours_is_no_shift():
    d = frost_shift(cold_until("00:00"), BASE_END, SUNRISE, 60, 180, TUN)
    assert d == FrostDecision(BASE_END, 180, None, 0.0)


def test_cold_that_clears_before_the_run_is_no_shift():
    # Run 06:30-07:30; cold until 06:00.
    d = frost_shift(cold_until("06:00"), BASE_END, SUNRISE, 60, 180, TUN)
    assert d.shift_minutes == 0 and d.end == BASE_END and not d.skip


def test_short_run_slides_just_past_the_cold():
    d = frost_shift(cold_until("08:00"), BASE_END, SUNRISE, 30, 180, TUN)
    assert (d.end, d.shift_minutes, d.clear_at) == (at("08:30"), 60.0, at("08:00"))
    assert d.cap_minutes == 30.0 and not d.skip


def test_long_run_stops_at_the_cap_and_shrinks_the_window():
    d = frost_shift(cold_until("08:00"), BASE_END, SUNRISE, 120, 180, TUN)
    assert (d.end, d.shift_minutes, d.cap_minutes) == (at("09:00"), 90.0, 60.0)


def test_cold_through_the_cap_skips():
    d = frost_shift(cold_until("09:00"), BASE_END, SUNRISE, 30, 180, TUN)
    assert d.skip and d.reason == "frost" and d.end == BASE_END


def test_cold_hours_after_the_cap_do_not_count():
    hourly = cold_until("08:00")
    hourly[9] = (at("09:00"), 30.0)  # cold again from the latest finish on
    d = frost_shift(hourly, BASE_END, SUNRISE, 30, 180, TUN)
    assert d.end == at("08:30") and not d.skip


def test_a_late_cold_spell_inside_the_cap_skips():
    # Warm 06:00-08:00, cold again 08:00-09:00: no clear run to the cap.
    hourly = cold_until("06:00")
    hourly[8] = (at("08:00"), 30.0)
    d = frost_shift(hourly, BASE_END, SUNRISE, 120, 180, TUN)
    assert d.skip


def test_the_cap_never_grows():
    d = frost_shift(cold_until("08:00"), BASE_END, SUNRISE, 30, 20, TUN)
    assert d.cap_minutes == 20


@pytest.mark.parametrize("hourly", [[], None])
def test_no_forecast_is_no_shift(hourly):
    d = frost_shift(hourly or [], BASE_END, SUNRISE, 60, 180, TUN)
    assert d.shift_minutes == 0 and not d.skip


def test_an_empty_plan_is_no_shift():
    d = frost_shift(cold_until(None), BASE_END, SUNRISE, 0, 180, TUN)
    assert d.shift_minutes == 0 and not d.skip


def test_dawn_anchored_level_still_caps_at_sunrise_plus_90():
    dawn_end = at("07:00")
    d = frost_shift(cold_until("08:00"), dawn_end, SUNRISE, 120, 180, TUN)
    assert (d.end, d.shift_minutes, d.cap_minutes) == (at("09:00"), 120.0, 60.0)


def test_a_replan_after_the_clearing_starts_from_now():
    # Restart at 08:10, cold cleared at 08:00: a 30 min run ends 08:40.
    d = frost_shift(cold_until("08:00"), BASE_END, SUNRISE, 30, 180, TUN,
                    now=at("08:10"))
    assert (d.end, d.cap_minutes, d.clear_at) == (at("08:40"), 30.0, at("08:00"))


def test_a_replan_past_the_latest_finish_has_no_window():
    d = frost_shift(cold_until("08:00"), BASE_END, SUNRISE, 30, 180, TUN,
                    now=at("09:10"))
    assert d.end == at("09:00") and d.cap_minutes == 0.0


def test_threshold_is_strictly_below():
    hourly = [(at(f"{h:02d}:00"), 38.0) for h in range(12)]
    assert frost_shift(hourly, BASE_END, SUNRISE, 60, 180, TUN).shift_minutes == 0


def test_parse_hourly_keeps_good_rows_sorted():
    raw = [["2026-11-02T06:00:00-04:00", 33], ["2026-11-02T05:00:00-04:00", "31.5"],
           ["2026-11-02T07:00:00", 40], ["garbage", 1], ["2026-11-02T08:00:00-04:00"],
           ["2026-11-02T09:00:00-04:00", "nan"], None]
    assert parse_hourly(raw) == [(at("05:00"), 31.5), (at("06:00"), 33.0)]


@pytest.mark.parametrize("raw", [None, "x", 3, {}])
def test_parse_hourly_rejects_non_lists(raw):
    assert parse_hourly(raw) == []
