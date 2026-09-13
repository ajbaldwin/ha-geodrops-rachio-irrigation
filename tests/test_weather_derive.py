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
