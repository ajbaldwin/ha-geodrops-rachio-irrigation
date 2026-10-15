"""Daily mean temperature (C) and humidity (%) from recorder long-term
statistics, for the dollar spot assessment (engine/disease_risk.py).

Long-term statistics outlive the recorder's purge of raw states, start from the
history a user already has (no warm-up), and their daily mean is exactly the
model's input. A sensor without state_class: measurement has none; the engine
then turns the signal off and logs why.
"""
from __future__ import annotations

import datetime as dt

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def async_daily_means(
        hass: HomeAssistant, temp_id: str, rh_id: str,
        first_day: dt.date, end_day: dt.date,
) -> list[tuple[dt.date, float | None, float | None]]:
    """[(local day, mean C, mean %RH)] for first_day <= day < end_day, oldest
    first. A day missing one statistic carries None for it."""
    start = dt_util.start_of_local_day(first_day)
    end = dt_util.start_of_local_day(end_day)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {temp_id, rh_id}, "day",
        {"temperature": UnitOfTemperature.CELSIUS}, {"mean"})
    by_day: dict[dt.date, list[float | None]] = {}
    for col, stat_id in ((0, temp_id), (1, rh_id)):
        for row in stats.get(stat_id, []):
            day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
            by_day.setdefault(day, [None, None])[col] = row.get("mean")
    return [(day, v[0], v[1]) for day, v in sorted(by_day.items())]
