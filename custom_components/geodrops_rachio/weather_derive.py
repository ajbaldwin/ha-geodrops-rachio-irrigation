from __future__ import annotations
import datetime as dt
from collections.abc import Sequence
from typing import Any


def overnight_mean(samples: Sequence[float | None]) -> float | None:
    vals = [s for s in samples if s is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def overnight_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The 20:00→06:00 overnight window containing `now`."""
    if now.hour < 6:
        start_day = now.date() - dt.timedelta(days=1)
    else:
        start_day = now.date()
    start = dt.datetime.combine(start_day, dt.time(20, 0), tzinfo=now.tzinfo)
    end = dt.datetime.combine(start_day + dt.timedelta(days=1), dt.time(6, 0),
                              tzinfo=now.tzinfo)
    return start, end


# The observed window is what the 06:00 calibration scores against the nightly's
# forecast. The nightly plans at 23:00 and its hourly forecast starts then, so
# the observed side covers the same 23:00 -> 06:00, not the evening before it.
OBSERVED_WINDOW_START_HOUR = 23
OBSERVED_WINDOW_END_HOUR = 6
OBSERVED_WINDOW_LABEL = (
    f"{OBSERVED_WINDOW_START_HOUR:02d}:00-{OBSERVED_WINDOW_END_HOUR:02d}:00")


def observed_overnight_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The observed overnight span (23:00→06:00) anchored to the most recent
    23:00 at or before `now` (backward-looking).

    Unlike `overnight_window`, this never points at a future night: a read at
    06:00 (when the scheduler's calibration runs) captures the night that just
    ended, and a daytime read reflects last night.
    """
    if now.hour >= OBSERVED_WINDOW_START_HOUR:
        start_day = now.date()
    else:
        start_day = now.date() - dt.timedelta(days=1)
    start = dt.datetime.combine(start_day, dt.time(OBSERVED_WINDOW_START_HOUR, 0),
                                tzinfo=now.tzinfo)
    end = dt.datetime.combine(start_day + dt.timedelta(days=1),
                              dt.time(OBSERVED_WINDOW_END_HOUR, 0), tzinfo=now.tzinfo)
    return start, end


def observed_overnight_mean(samples: list[tuple[dt.datetime, float]],
                            now: dt.datetime) -> float | None:
    """Time-weighted mean over the observed overnight window for `now`.

    `samples` are (when the reading arrived, value), oldest first. Each reading
    holds until the next one, so the latest reading before the window carries
    into it. Weighting by duration rather than averaging the change events
    matters: a steady reading (calm air, RH pinned at saturation) reports few
    changes, and an event average would under-count exactly those hours. None
    until some reading has held for a moment inside the window.
    """
    start, end = observed_overnight_window(now)
    upto = min(now, end)
    total = weight = 0.0
    for i, (t, v) in enumerate(samples):
        held_to = samples[i + 1][0] if i + 1 < len(samples) else upto
        seconds = (min(held_to, upto) - max(t, start)).total_seconds()
        if seconds > 0:
            total += v * seconds
            weight += seconds
    return total / weight if weight > 0 else None


def prune_observed(samples: list[tuple[dt.datetime, float]],
                   now: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """The samples still needed for `now`'s window: those inside it, plus the
    latest one before it (its value is in effect when the window opens)."""
    start, _end = observed_overnight_window(now)
    before = [s for s in samples if s[0] < start]
    return before[-1:] + [s for s in samples if s[0] >= start]


def _in_span(periods: list[dict[str, Any]], field: str, start: dt.datetime,
             end: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """(time, value) of each forecast period from `start` (inclusive) to `end`
    that carries `field`. A naive period time is taken in the span's zone."""
    out: list[tuple[dt.datetime, float]] = []
    for p in periods:
        ts = p.get("datetime")
        if ts is None or p.get(field) is None:
            continue
        t = dt.datetime.fromisoformat(ts)
        if t.tzinfo is None and start.tzinfo is not None:
            t = t.replace(tzinfo=start.tzinfo)
        elif t.tzinfo is not None and start.tzinfo is None:
            t = t.replace(tzinfo=None)
        if start <= t < end:
            out.append((t, float(p[field])))
    return out


def overnight_forecast_mean(periods: list[dict[str, Any]], field: str,
                            now: dt.datetime) -> float | None:
    start, end = overnight_window(now)
    return overnight_mean([v for _t, v in _in_span(periods, field, start, end)])


# The frost shift searches the morning up to noon: the latest finish it allows
# (sunrise + its cap) falls well before that.
MORNING_END_HOUR = 12


def morning_span(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """00:00→12:00 of the coming morning: today's until noon, tomorrow's
    after."""
    day = now.date() if now.hour < MORNING_END_HOUR else (
        now.date() + dt.timedelta(days=1))
    return (dt.datetime.combine(day, dt.time(0, 0), tzinfo=now.tzinfo),
            dt.datetime.combine(day, dt.time(MORNING_END_HOUR, 0),
                                tzinfo=now.tzinfo))


def morning_hourly(periods: list[dict[str, Any]],
                   now: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """The forecast temperature of each hour in morning_span(now), oldest
    first, in the forecast's own unit."""
    start, end = morning_span(now)
    return sorted(_in_span(periods, "temperature", start, end))
