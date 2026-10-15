"""disease_stats reads daily means from recorder long-term statistics."""
import datetime as dt
from unittest.mock import patch

from homeassistant.util import dt as dt_util

from custom_components.geodrops_rachio import disease_stats


class _FakeRecorder:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _ts(day: dt.date) -> float:
    return dt_util.start_of_local_day(day).timestamp()


async def test_daily_means_per_local_day(hass):
    d1, d2 = dt.date(2026, 10, 13), dt.date(2026, 10, 14)
    rows = {
        "sensor.t": [{"start": _ts(d1), "mean": 15.0}, {"start": _ts(d2), "mean": 17.0}],
        "sensor.rh": [{"start": _ts(d1), "mean": 85.0}],
    }
    seen = {}

    def fake_stats(hass_, start, end, ids, period, units, types):
        seen.update(start=start, end=end, ids=ids, period=period,
                    units=units, types=types)
        return rows

    with patch.object(disease_stats, "get_instance", return_value=_FakeRecorder()), \
         patch.object(disease_stats, "statistics_during_period", fake_stats):
        out = await disease_stats.async_daily_means(
            hass, "sensor.t", "sensor.rh", dt.date(2026, 10, 10), dt.date(2026, 10, 15))
    assert out == [(d1, 15.0, 85.0), (d2, 17.0, None)]
    assert seen["ids"] == {"sensor.t", "sensor.rh"} and seen["period"] == "day"
    assert seen["units"] == {"temperature": "°C"} and seen["types"] == {"mean"}
    assert seen["start"] == dt_util.start_of_local_day(dt.date(2026, 10, 10))
    assert seen["end"] == dt_util.start_of_local_day(dt.date(2026, 10, 15))


async def test_no_statistics_is_an_empty_list(hass):
    with patch.object(disease_stats, "get_instance", return_value=_FakeRecorder()), \
         patch.object(disease_stats, "statistics_during_period",
                      lambda *_a: {}):
        out = await disease_stats.async_daily_means(
            hass, "sensor.t", "sensor.rh", dt.date(2026, 10, 10), dt.date(2026, 10, 15))
    assert out == []
