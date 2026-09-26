import datetime as dt

import pytest
from custom_components.geodrops_rachio import weather_derive


def test_overnight_mean_basic():
    assert weather_derive.overnight_mean([10.0, 20.0, 30.0]) == 20.0
    assert weather_derive.overnight_mean([None, 10.0, None, 30.0]) == 20.0
    assert weather_derive.overnight_mean([]) is None
    assert weather_derive.overnight_mean([None, None]) is None


def test_overnight_window_evening():
    now = dt.datetime(2026, 9, 12, 22, 0)
    start, end = weather_derive.overnight_window(now)
    assert start == dt.datetime(2026, 9, 12, 20, 0)
    assert end == dt.datetime(2026, 9, 13, 6, 0)


def test_overnight_window_after_midnight():
    now = dt.datetime(2026, 9, 13, 2, 0)
    start, end = weather_derive.overnight_window(now)
    assert start == dt.datetime(2026, 9, 12, 20, 0)
    assert end == dt.datetime(2026, 9, 13, 6, 0)


def test_overnight_forecast_mean_filters_window():
    now = dt.datetime(2026, 9, 12, 19, 0)
    periods = [
        {"datetime": "2026-09-12T18:00:00", "temperature": 100.0},  # before window
        {"datetime": "2026-09-12T21:00:00", "temperature": 10.0},   # in
        {"datetime": "2026-09-13T03:00:00", "temperature": 20.0},   # in
        {"datetime": "2026-09-13T07:00:00", "temperature": 100.0},  # after
    ]
    assert weather_derive.overnight_forecast_mean(periods, "temperature", now) == 15.0


def test_overnight_forecast_mean_handles_aware_datetimes():
    now = dt.datetime(2026, 9, 12, 19, 0, tzinfo=dt.timezone.utc)
    periods = [
        {"datetime": "2026-09-12T18:00:00+00:00", "temperature": 100.0},  # before window
        {"datetime": "2026-09-12T21:00:00+00:00", "temperature": 10.0},   # in
        {"datetime": "2026-09-13T03:00:00+00:00", "temperature": 20.0},   # in
        {"datetime": "2026-09-13T07:00:00+00:00", "temperature": 100.0},  # after
    ]
    assert weather_derive.overnight_forecast_mean(periods, "temperature", now) == 15.0


D = dt.datetime


def test_observed_overnight_window_matches_the_nightly_forecast():
    # The nightly plans at 23:00 and reads a forecast that starts then, so the
    # observed window scored against it is 23:00 -> 06:00, not 20:00 -> 06:00.
    # A 06:00 read captures LAST night.
    start, end = weather_derive.observed_overnight_window(D(2026, 9, 13, 6, 0))
    assert (start, end) == (D(2026, 9, 12, 23, 0), D(2026, 9, 13, 6, 0))
    assert weather_derive.OBSERVED_WINDOW_LABEL == "23:00-06:00"


def test_observed_overnight_window_evening_and_daytime():
    # 23:00 or later -> tonight's window.
    s, e = weather_derive.observed_overnight_window(D(2026, 9, 12, 23, 30))
    assert (s, e) == (D(2026, 9, 12, 23, 0), D(2026, 9, 13, 6, 0))
    # Earlier in the evening, and daytime -> last night (backward-looking).
    for now in (D(2026, 9, 13, 14, 0), D(2026, 9, 13, 22, 59)):
        s, e = weather_derive.observed_overnight_window(now)
        assert (s, e) == (D(2026, 9, 12, 23, 0), D(2026, 9, 13, 6, 0))


def test_observed_overnight_mean_is_time_weighted():
    """A reading counts for as long as it held. A calm, saturated night reports
    few state changes; averaging the change events would under-weight exactly
    the humid/stagnant hours the calibration is trying to score."""
    samples = [
        (D(2026, 9, 12, 22, 0), 10.0),   # carried in: in effect from 23:00
        (D(2026, 9, 13, 5, 0), 30.0),    # the last hour
    ]
    mean = weather_derive.observed_overnight_mean(samples, D(2026, 9, 13, 6, 0))
    assert mean == pytest.approx((10.0 * 6 + 30.0 * 1) / 7)


def test_observed_overnight_mean_mid_window_and_after_it():
    samples = [(D(2026, 9, 12, 23, 0), 10.0), (D(2026, 9, 13, 1, 0), 20.0)]
    # 02:00: 10 for 2h, 20 for 1h so far.
    assert weather_derive.observed_overnight_mean(
        samples, D(2026, 9, 13, 2, 0)) == pytest.approx(40.0 / 3)
    # Daytime: the window is closed at 06:00 (10 for 2h, 20 for 5h).
    assert weather_derive.observed_overnight_mean(
        samples, D(2026, 9, 13, 14, 0)) == pytest.approx(120.0 / 7)


def test_observed_overnight_mean_without_a_reading_is_none():
    # Nothing at or before the window: no value can be known.
    assert weather_derive.observed_overnight_mean([], D(2026, 9, 13, 6, 0)) is None
    # A reading only at the very instant asked: nothing has elapsed yet.
    assert weather_derive.observed_overnight_mean(
        [(D(2026, 9, 12, 23, 0), 5.0)], D(2026, 9, 12, 23, 0)) is None


def test_prune_keeps_the_reading_carried_into_the_window():
    samples = [(D(2026, 9, 12, 18, 0), 1.0), (D(2026, 9, 12, 22, 0), 2.0),
               (D(2026, 9, 13, 1, 0), 3.0)]
    kept = weather_derive.prune_observed(samples, D(2026, 9, 13, 2, 0))
    assert kept == samples[1:]
