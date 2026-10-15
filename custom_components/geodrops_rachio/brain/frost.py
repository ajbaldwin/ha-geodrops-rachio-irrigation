"""Frost shift for Automatic watering time. Pure Python.

Early morning, in the dew period, is the right time to water in every season;
only cold justifies watering later. Near freezing a pre-dawn run ices turf,
walks and drives (Oklahoma State: not below 40 °F) and can damage the system
(UGA: not at or below 32 °F). So the finish stays at the level's sun anchor
unless a forecast hour the run would overlap is cold. Then it slides later,
just far enough for the run to follow the cold, and never past sunrise plus a
short cap: once the dew dries, watering extends leaf wetness. Cold lasting
through the cap skips the night.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import Tunables

_HOUR = dt.timedelta(hours=1)


@dataclass(frozen=True)
class FrostDecision:
    end: dt.datetime        # when the run should finish
    cap_minutes: float      # the window cap to plan with
    # Start of the first forecast hour from which every hour up to the latest
    # finish is warm; None when no cold hour touches the run.
    clear_at: dt.datetime | None
    shift_minutes: float    # end minus the anchor-based end; 0 = unchanged
    skip: bool = False
    reason: str = ""


def parse_hourly(raw: Any) -> list[tuple[dt.datetime, float]]:
    """The morning forecast sensor's `hourly` attribute ([[iso hour start,
    °F], ...]) as sorted (tz-aware hour start, °F) pairs. Malformed entries
    are dropped, so anything unusable comes back as []."""
    out: list[tuple[dt.datetime, float]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        try:
            start = dt.datetime.fromisoformat(str(item[0]))
            temp = float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if start.tzinfo is None or temp != temp:  # naive, or NaN
            continue
        out.append((start, temp))
    return sorted(out)


def frost_shift(hourly: Sequence[tuple[dt.datetime, float]],
                base_end: dt.datetime, sunrise: dt.datetime,
                span_minutes: float, cap_minutes: float,
                tun: Tunables, now: dt.datetime | None = None) -> FrostDecision:
    """Where tonight's run should finish, given the morning's hourly forecast.

    `base_end` is the anchor-based finish (anchor minus offset), `span_minutes`
    how long the built plan runs, `cap_minutes` the disease-window cap. Each
    forecast hour's temperature holds for that hour, and an hour below
    `frost_temp_f` is cold.

    No cold hour overlapping the run, no forecast, or an empty plan: unchanged.
    Otherwise the run finishes `span_minutes` after the cold clears, but no
    earlier than `base_end` and no later than sunrise + frost_max_delay_minutes
    (measured from sunrise even at dawn-anchored levels). The cap shrinks to
    the time between the clearing and the finish; the caller rebuilds the plan
    when that no longer fits it. Cold up to the latest finish is a skip.

    `now`, for a re-plan made after the cold cleared (a restart mid-window):
    the run cannot start before it, so it counts as the clearing for the
    finish and the cap.
    """
    unchanged = FrostDecision(base_end, cap_minutes, None, 0.0)
    if not hourly or span_minutes <= 0:
        return unchanged
    latest_end = max(
        sunrise + dt.timedelta(minutes=tun.frost_max_delay_minutes), base_end)
    run_start = base_end - dt.timedelta(minutes=span_minutes)
    cold = [start for start, temp in hourly if temp < tun.frost_temp_f]
    if not any(start < base_end and start + _HOUR > run_start for start in cold):
        return unchanged
    # An hour overlapping the run starts before base_end <= latest_end, so
    # there is always a cold hour to clear here.
    clear_at = max(start for start in cold if start < latest_end) + _HOUR
    if clear_at >= latest_end:
        return FrostDecision(base_end, cap_minutes, clear_at, 0.0,
                             skip=True, reason="frost")
    first_start = clear_at if now is None else max(clear_at, now)
    end = min(max(first_start + dt.timedelta(minutes=span_minutes), base_end),
              latest_end)
    cap = max(0.0, min(cap_minutes, (end - first_start).total_seconds() / 60.0))
    return FrostDecision(end, cap, clear_at,
                         (end - base_end).total_seconds() / 60.0)
