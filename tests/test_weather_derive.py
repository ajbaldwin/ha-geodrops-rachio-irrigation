import datetime as dt
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


def test_observed_overnight_window_at_calibration_time():
    # 06:00 read must capture LAST night (prev 20:00 -> today 06:00), not tonight.
    start, end = weather_derive.observed_overnight_window(dt.datetime(2026, 9, 13, 6, 0))
    assert start == dt.datetime(2026, 9, 12, 20, 0)
    assert end == dt.datetime(2026, 9, 13, 6, 0)


def test_observed_overnight_window_evening_and_daytime():
    # After 20:00 -> tonight's window.
    s, e = weather_derive.observed_overnight_window(dt.datetime(2026, 9, 12, 21, 0))
    assert (s, e) == (dt.datetime(2026, 9, 12, 20, 0), dt.datetime(2026, 9, 13, 6, 0))
    # Daytime -> last night's window (non-empty, backward-looking).
    s, e = weather_derive.observed_overnight_window(dt.datetime(2026, 9, 13, 14, 0))
    assert (s, e) == (dt.datetime(2026, 9, 12, 20, 0), dt.datetime(2026, 9, 13, 6, 0))


def test_observed_overnight_mean_filters_to_window():
    now = dt.datetime(2026, 9, 13, 6, 0)
    samples = [
        (dt.datetime(2026, 9, 12, 18, 0), 100.0),  # before 20:00 -> excluded
        (dt.datetime(2026, 9, 12, 21, 0), 10.0),   # in
        (dt.datetime(2026, 9, 13, 5, 0), 20.0),    # in
    ]
    assert weather_derive.observed_overnight_mean(samples, now) == 15.0
    # No samples in window -> None.
    assert weather_derive.observed_overnight_mean(
        [(dt.datetime(2026, 9, 12, 18, 0), 5.0)], now) is None
