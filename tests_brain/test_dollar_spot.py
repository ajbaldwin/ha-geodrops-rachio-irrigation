import datetime as dt

import pytest

from brain import dollar_spot as ds

TODAY = dt.date(2026, 10, 15)


def days(n, t=15.0, rh=80.0, start=1):
    return [(TODAY - dt.timedelta(days=i), t, rh) for i in range(start, start + n)]


def test_probability_known_values():
    assert ds.probability_pct(20.0, 80.0) == pytest.approx(40.4, abs=0.05)
    assert ds.probability_pct(15.0, 75.0) == pytest.approx(14.2, abs=0.05)


def test_probability_valid_only_between_10_and_35_c():
    assert ds.probability_pct(9.9, 90.0) is None
    assert ds.probability_pct(35.1, 90.0) is None
    assert ds.probability_pct(10.0, 90.0) is not None
    assert ds.probability_pct(35.0, 90.0) is not None


def test_five_day_means_uses_last_five_complete_days():
    daily = days(5, t=20.0, rh=80.0) + [(TODAY, 99.0, 99.0)]  # today excluded
    assert ds.five_day_means(daily, TODAY) == (20.0, 80.0, 5)


def test_five_day_means_tolerates_one_gap():
    daily = days(5)
    daily[2] = (daily[2][0], None, 80.0)
    assert ds.five_day_means(daily, TODAY) == (15.0, 80.0, 4)


def test_five_day_means_needs_four_days():
    assert ds.five_day_means(days(3), TODAY) is None


def test_five_day_means_ignores_days_older_than_five():
    daily = days(5, t=20.0) + days(3, t=0.0, start=6)
    assert ds.five_day_means(daily, TODAY)[0] == 20.0


def test_assess_builds_a_risk():
    risk = ds.assess(days(5, t=20.0, rh=80.0), TODAY)
    assert risk is not None
    assert risk.pct == pytest.approx(40.4, abs=0.05)
    assert (risk.t5_c, risk.rh5, risk.days) == (20.0, 80.0, 5)
    assert risk.as_of == "2026-10-14"


def test_assess_out_of_range_is_a_risk_with_no_probability():
    risk = ds.assess(days(5, t=5.0), TODAY)
    assert risk is not None and risk.pct is None


def test_assess_without_enough_days_is_none():
    assert ds.assess(days(2), TODAY) is None
