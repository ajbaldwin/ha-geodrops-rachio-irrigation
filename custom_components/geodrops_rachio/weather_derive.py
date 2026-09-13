from __future__ import annotations
import datetime as dt


def overnight_mean(samples: list[float | None]) -> float | None:
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


def overnight_forecast_mean(periods: list[dict], field: str,
                            now: dt.datetime) -> float | None:
    start, end = overnight_window(now)
    vals: list[float] = []
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
            vals.append(float(p[field]))
    return overnight_mean(vals)
