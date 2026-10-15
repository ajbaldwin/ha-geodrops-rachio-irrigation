"""DiseaseRiskMixin: the hourly dollar spot refresh fails open and logs once."""
import datetime as dt
import logging

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.base import EngineBase
from custom_components.geodrops_rachio.engine.disease_risk import DiseaseRiskMixin
from custom_components.geodrops_rachio.engine.store import EngineStore
from tests.engine.scenario import T_PLAN, _nosave, entry_data, fake_fetch, populate
from tests.engine.world import FakePort, FakeWorld


def _eng(freezer, fetch, data=None):
    freezer.move_to(T_PLAN)
    world = FakeWorld(freezer)
    data = data or entry_data()
    populate(world, data)
    cls = type("TestEngine", (DiseaseRiskMixin, EngineBase), {})
    eng = cls(FakePort(world), EngineStore({}, _nosave),
              lambda: build_config(data), fake_fetch(world),
              fetch_daily_means=fetch)
    return world, eng


def _fetcher(t=20.0, rh=80.0, n=5, calls=None):
    async def fetch(temp_id, rh_id, first, end):
        if calls is not None:
            calls.append((temp_id, rh_id, first, end))
        return [(end - dt.timedelta(days=i), t, rh) for i in range(1, n + 1)]
    return fetch


async def test_refresh_reads_the_bound_sensors_for_five_days(freezer):
    calls = []
    _w, eng = _eng(freezer, _fetcher(calls=calls))
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is not None and round(eng.disease_risk.pct, 1) == 40.4
    temp_id, rh_id, first, end = calls[0]
    assert (temp_id, rh_id) == ("sensor.tempest_sensor_temperature",
                                "sensor.tempest_sensor_humidity")
    assert end == eng.port.now().date() and (end - first).days == 5


async def test_no_fetcher_or_too_few_days_is_none(freezer):
    _w, eng = _eng(freezer, None)
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None
    _w, eng = _eng(freezer, _fetcher(n=2))
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None


async def test_disabled_tunable_is_none_without_fetching(freezer):
    calls = []
    _w, eng = _eng(freezer, _fetcher(calls=calls),
                   entry_data(overrides="dollar_spot_enabled: false"))
    await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None and calls == []


async def test_fetch_error_fails_open_and_logs_once(freezer, caplog):
    async def boom(*_):
        raise RuntimeError("recorder not loaded")
    _w, eng = _eng(freezer, boom)
    with caplog.at_level(logging.INFO):
        await eng.async_refresh_disease_risk()
        await eng.async_refresh_disease_risk()
    assert eng.disease_risk is None
    assert caplog.text.count("Dollar spot pressure is off") == 1


async def test_recovery_is_logged_once(freezer, caplog):
    state = {"fail": True}

    async def flaky(temp_id, rh_id, first, end):
        if state["fail"]:
            raise RuntimeError("recorder not loaded")
        return await _fetcher()(temp_id, rh_id, first, end)
    _w, eng = _eng(freezer, flaky)
    with caplog.at_level(logging.INFO):
        await eng.async_refresh_disease_risk()
        state["fail"] = False
        await eng.async_refresh_disease_risk()
        await eng.async_refresh_disease_risk()
    assert eng.disease_risk is not None
    assert caplog.text.count("Dollar spot pressure is on again") == 1


async def test_listeners_notified_only_on_change(freezer):
    _w, eng = _eng(freezer, _fetcher())
    seen = []
    eng.add_listener(lambda: seen.append(1))
    await eng.async_refresh_disease_risk()
    await eng.async_refresh_disease_risk()
    assert len(seen) == 1
