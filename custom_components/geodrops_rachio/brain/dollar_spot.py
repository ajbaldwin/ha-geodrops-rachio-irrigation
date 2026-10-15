"""Smith-Kerns dollar spot probability. Pure Python.

The model (UW-Madison Turfgrass Diagnostic Lab; the one behind Purdue's
Turfgrass Disease Prediction Tool) is a logistic regression on the 5-day moving
averages of daily mean relative humidity and air temperature:

    logit = -11.4041 + 0.0894 * RH5 + 0.1932 * T5(C)
    P     = e^logit / (1 + e^logit)

It is only valid for 10 C <= T5 <= 35 C; outside that the fungus is inactive.
Validated on creeping bentgrass golf turf, so the threshold applied to it is a
tunable starting point for lawns, not a calibrated value.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass

INTERCEPT = -11.4041
RH_COEF = 0.0894
TEMP_COEF = 0.1932
MIN_T5_C = 10.0
MAX_T5_C = 35.0
WINDOW_DAYS = 5
# A station offline for a day must not switch the signal off for five.
MIN_DAYS = 4

DailyMeans = Sequence[tuple[dt.date, float | None, float | None]]


@dataclass(frozen=True)
class DiseaseRisk:
    """The dollar spot assessment over the complete days up to `as_of`.

    `pct` is None when T5 is outside the model's valid range (not active)."""
    pct: float | None
    t5_c: float
    rh5: float
    days: int
    as_of: str  # ISO date of the most recent day used


def probability_pct(t5_c: float, rh5: float) -> float | None:
    """Smith-Kerns probability in percent; None outside 10-35 C."""
    if not MIN_T5_C <= t5_c <= MAX_T5_C:
        return None
    logit = INTERCEPT + RH_COEF * rh5 + TEMP_COEF * t5_c
    return 100.0 / (1.0 + math.exp(-logit))


def five_day_means(daily: DailyMeans,
                   today: dt.date) -> tuple[float, float, int] | None:
    """(T5 C, RH5 %, days used) over the WINDOW_DAYS complete local days before
    `today`. Complete days only, so the 23:00 plan and a 02:00 restart agree.
    None when fewer than MIN_DAYS of them have both values."""
    window = {today - dt.timedelta(days=i) for i in range(1, WINDOW_DAYS + 1)}
    by_day: dict[dt.date, tuple[float, float]] = {}
    for day, t_c, rh in daily:
        if day in window and t_c is not None and rh is not None:
            by_day[day] = (t_c, rh)
    if len(by_day) < MIN_DAYS:
        return None
    n = len(by_day)
    return (sum(v[0] for v in by_day.values()) / n,
            sum(v[1] for v in by_day.values()) / n, n)


def assess(daily: DailyMeans, today: dt.date) -> DiseaseRisk | None:
    """The risk for `today`, or None when there are not enough days."""
    means = five_day_means(daily, today)
    if means is None:
        return None
    t5_c, rh5, n = means
    return DiseaseRisk(pct=probability_pct(t5_c, rh5), t5_c=t5_c, rh5=rh5,
                       days=n, as_of=(today - dt.timedelta(days=1)).isoformat())
