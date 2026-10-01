from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

DATA = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""}


@pytest.fixture(autouse=True)
def _config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture(autouse=True)
def _no_startup_sleep():
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
               AsyncMock()):
        yield


async def test_setup_and_unload_entry(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.scheduler is not None
    scheduler = entry.runtime_data.scheduler
    with patch.object(scheduler, "async_shutdown",
                      AsyncMock(wraps=scheduler.async_shutdown)) as shutdown:
        assert await hass.config_entries.async_unload(entry.entry_id)
    shutdown.assert_awaited_once()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retires_delivered_pyscript(hass, tmp_path, enable_pyscript_and_rachio):
    ps = tmp_path / "pyscript"
    ps.mkdir()
    (ps / "geodrops_rachio.py").write_text("# legacy")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not (ps / "geodrops_rachio.py").exists()


async def test_setup_does_not_need_pyscript(hass):
    hass.config.components.add("rachio")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_bad_overrides_not_ready(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "advanced_overrides": "- a"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    # The translated reason, shown on the integration's card.
    assert entry.reason.startswith("The advanced overrides are not valid: ")
    assert entry.reason.endswith("Fix them under Configure → Advanced")


async def test_nightly_trigger_starts_run(hass, enable_pyscript_and_rachio):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._start_run",
               AsyncMock()) as start:
        assert await hass.config_entries.async_setup(entry.entry_id)
        now = dt_util.now()
        async_fire_time_changed(hass, now.replace(hour=23, minute=0, second=0, microsecond=0)
                                + timedelta(days=1))
        await hass.async_block_till_done()
    start.assert_awaited_with(True, "nightly")


async def test_reload_listener_honors_suppress_flag(hass, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        # Flag set -> update fires the listener but it must NOT reload.
        entry.runtime_data.suppress_reload = True
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "advanced_overrides": "a: 1"})
        await hass.async_block_till_done()
        assert reloads == []

        # Flag cleared -> the next data change reloads exactly once.
        entry.runtime_data.suppress_reload = False
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "advanced_overrides": "a: 2"})
        await hass.async_block_till_done()
        assert reloads == [entry.entry_id]


async def test_options_flow_defers_reload_until_finish(hass, enable_pyscript_and_rachio):
    from unittest.mock import patch as _patch
    from custom_components.geodrops_rachio.const import DOMAIN
    zone = {"key": "front", "rachio_switch": "switch.front",
            "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
            "quality_sensors": [], "target_range": "moist",
            "runtime_minutes": 45, "refill_depth_mm": 7.11}
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"notify_service": "notify.phone", "rachio_device_name": "Main House"},
        "zones": [dict(zone)], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    with _patch.object(hass.config_entries, "async_reload", _fake_reload):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"key": "back", "rachio_switch": "switch.back",
             "dominant_sensor": "sensor.d2", "state_sensor": "sensor.s2",
             "quality_sensors": [], "target_range": "moist",
             "runtime_minutes": 20, "refill_depth_mm": 10})
        await hass.async_block_till_done()
        # Persisted, but the guard held off the reload.
        assert [z["key"] for z in entry.data["zones"]] == ["front", "back"]
        assert reloads == []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        await hass.async_block_till_done()
    assert reloads == [entry.entry_id]           # exactly one, at finish


async def test_removing_zone_purges_its_device(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from tests.conftest import zone_device
    from custom_components.geodrops_rachio.const import DOMAIN
    def _zone(k):
        return {"key": k, "rachio_switch": "switch.x", "dominant_sensor": "sensor.d",
                "state_sensor": "sensor.s", "quality_sensors": [], "target_range": "moist",
                "runtime_minutes": 20, "refill_depth_mm": 10}
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [_zone("front"), _zone("back")],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert zone_device(hass, entry, "back")

    hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": [_zone("front")]})
    await hass.async_block_till_done()  # triggers reload via existing options listener
    assert zone_device(hass, entry, "back") is None
    assert zone_device(hass, entry, "front")


def _full_entry_data():
    from tests.engine.scenario import entry_data
    return entry_data()


async def test_ha_stop_safety_stops_a_watering_run(hass, enable_pyscript_and_rachio, caplog):
    """HA's own stop path: the stage-1 shutdown job must see the run watering
    (HA cancels background tasks, the run included, only after stage 1) and
    stop the device + every zone, blocking — even with the startup task still
    in its initial sleep. Unloading afterwards logs no error."""
    import asyncio
    from homeassistant.core import CoreState
    from pytest_homeassistant_custom_component.common import async_mock_service

    watering = asyncio.Event()

    async def fake_plan_and_run(self, wait, trigger):
        self._current_cfg = self._load_cfg()
        self._current_bindings = self._current_cfg.bindings
        self._watering_active = True
        watering.set()
        try:
            await asyncio.Event().wait()           # valves open, run in progress
        finally:
            self._watering_active = False

    async def slow_startup(self):
        await asyncio.Event().wait()               # the real one sleeps 30 s first

    from custom_components.geodrops_rachio.engine.port import HassPort
    port_calls = []
    real_call = HassPort.call

    async def spy_call(self, domain, service, data, *, blocking=False):
        port_calls.append((domain, service, blocking))
        await real_call(self, domain, service, data, blocking=blocking)

    sched = "custom_components.geodrops_rachio.engine.scheduler.Scheduler."
    entry = MockConfigEntry(domain=DOMAIN, data=_full_entry_data())
    entry.add_to_hass(hass)
    with patch(sched + "_plan_and_run", fake_plan_and_run), \
            patch(sched + "_on_startup", slow_startup), \
            patch.object(HassPort, "call", spy_call):
        assert await hass.config_entries.async_setup(entry.entry_id)
        # After setup: loading the switch platform registers the real
        # switch.turn_off, which would replace an earlier mock.
        stops = async_mock_service(hass, "rachio", "stop_watering")
        offs = async_mock_service(hass, "switch", "turn_off")
        scheduler = entry.runtime_data.scheduler
        await scheduler.async_run_now()
        await watering.wait()
        assert scheduler.startup_task is not None and not scheduler.startup_task.done()

        await hass.async_stop()

    assert hass.state is CoreState.stopped
    assert [c.data for c in stops] == [{"devices": "Main House"}]
    assert [c.data for c in offs] == [{"entity_id": "switch.front_zone"},
                                      {"entity_id": "switch.back_zone"}]
    assert port_calls == [("rachio", "stop_watering", True),
                          ("switch", "turn_off", True), ("switch", "turn_off", True)]
    assert scheduler.run_task is None and scheduler.startup_task is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []


async def test_options_update_without_changes_does_not_reload(
        hass, enable_pyscript_and_rachio):
    import copy
    from custom_components.geodrops_rachio import async_reload_if_changed
    entry = MockConfigEntry(domain=DOMAIN, data=_full_entry_data())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = entry.runtime_data
    scheduler = store.scheduler
    original = copy.deepcopy(dict(entry.data))

    with patch.object(scheduler, "async_shutdown",
                      AsyncMock(wraps=scheduler.async_shutdown)) as shutdown:
        # The update listener fires (a title change counts as an entry update)
        # but data and options equal the setup snapshot: no reload.
        hass.config_entries.async_update_entry(entry, title="Renamed")
        await hass.async_block_till_done()
        shutdown.assert_not_awaited()
        assert entry.runtime_data.scheduler is scheduler

        # Edited and reverted while the options dialog held reloads off: "Done"
        # finds the config back where it started and leaves the run alone.
        store.suppress_reload = True
        edited = copy.deepcopy(original)
        edited["zones"][0]["runtime_minutes"] = 41
        hass.config_entries.async_update_entry(entry, data=edited)
        hass.config_entries.async_update_entry(entry, data=copy.deepcopy(original))
        await hass.async_block_till_done()
        store.suppress_reload = False
        assert not await async_reload_if_changed(hass, entry)
        await hass.async_block_till_done()
        shutdown.assert_not_awaited()
        assert entry.runtime_data.scheduler is scheduler

        # A real change reloads, shutting the running scheduler down.
        hass.config_entries.async_update_entry(entry, data=edited)
        await hass.async_block_till_done()
        shutdown.assert_awaited_once()
    assert entry.runtime_data.scheduler is not scheduler

    # An options change is a change too (the snapshot covers options).
    scheduler = entry.runtime_data.scheduler
    with patch.object(scheduler, "async_shutdown",
                      AsyncMock(wraps=scheduler.async_shutdown)) as shutdown:
        hass.config_entries.async_update_entry(entry, options={"x": 1})
        await hass.async_block_till_done()
        shutdown.assert_awaited_once()


async def test_options_flow_done_without_edits_does_not_reload(
        hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=_full_entry_data())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert reloads == []
    assert entry.runtime_data.suppress_reload is False


async def test_setup_survives_failing_pyscript_reload(
        hass, tmp_path, enable_pyscript_and_rachio, caplog):
    ps = tmp_path / "pyscript"
    ps.mkdir()
    (ps / "geodrops_rachio.py").write_text("# legacy")

    async def _boom(_call):
        raise RuntimeError("pyscript reload exploded")
    hass.services.async_register("pyscript", "reload", _boom)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert not (ps / "geodrops_rachio.py").exists()
    assert any("pyscript.reload failed" in r.getMessage() for r in caplog.records)


async def test_unload_stops_the_scheduler_before_its_platforms(
        hass, enable_pyscript_and_rachio):
    """The safety stop must run while the entities it reads still exist."""
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    scheduler = entry.runtime_data.scheduler
    order = []
    real_shutdown = scheduler.async_shutdown
    real_unload = hass.config_entries.async_unload_platforms

    async def shutdown():
        order.append("scheduler")
        await real_shutdown()

    async def unload_platforms(*args):
        order.append("platforms")
        return await real_unload(*args)

    with patch.object(scheduler, "async_shutdown", shutdown), \
            patch.object(hass.config_entries, "async_unload_platforms", unload_platforms):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert order[:2] == ["scheduler", "platforms"]


async def _open_options_then_close(hass, entry, edit):
    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert entry.runtime_data.suppress_reload is True
        if edit:
            # A sub-step persists an edit while the guard is up ...
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "advanced_overrides": "a: 1"})
            await hass.async_block_till_done()
            assert reloads == []
        # ... then the admin closes the dialog instead of pressing Done.
        hass.config_entries.options.async_abort(result["flow_id"])
        await hass.async_block_till_done()
    return reloads


async def test_options_closed_without_done_applies_persisted_edits(
        hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=_full_entry_data())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reloads = await _open_options_then_close(hass, entry, edit=True)
    assert reloads == [entry.entry_id]
    assert entry.runtime_data.suppress_reload is False


async def test_options_closed_without_edits_clears_guard_without_reload(
        hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=_full_entry_data())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reloads = await _open_options_then_close(hass, entry, edit=False)
    assert reloads == []
    assert entry.runtime_data.suppress_reload is False


async def test_reload_if_changed_reloads_an_entry_that_is_not_loaded(
        hass, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio import async_reload_if_changed
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # e.g. a failed platform unload: the setup snapshot lingers in hass.data.
    entry.mock_state(hass, ConfigEntryState.FAILED_UNLOAD)
    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        assert await async_reload_if_changed(hass, entry) is True
    assert reloads == [entry.entry_id]
    entry.mock_state(hass, ConfigEntryState.LOADED)


async def test_failed_platform_unload_is_logged(hass, enable_pyscript_and_rachio, caplog):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    with patch.object(hass.config_entries, "async_unload_platforms",
                      AsyncMock(return_value=False)):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert any(r.levelname == "WARNING" and "did not unload" in r.getMessage()
               for r in caplog.records)


async def test_removing_entry_deletes_its_store(hass, hass_storage, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await entry.runtime_data.scheduler.store.write("efficacy", {"x": {}})
    key = f"{DOMAIN}.{entry.entry_id}"
    assert key in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_stop_button_works_without_logbook(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service("logbook", "log")
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.geodrops_rachio_stop"}, blocking=True)
    assert entry.runtime_data.scheduler._manual_stop is True


async def test_rachio_native_run_is_tracked_through_state_events(
        hass, enable_pyscript_and_rachio):
    """A zone switch flipping in HA's state machine, outside our runs, reaches
    the scheduler as a Rachio session; unloading stops listening."""
    hass.states.async_set("switch.front_zone", "off")
    entry = MockConfigEntry(domain=DOMAIN, data=_full_entry_data())
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.engine.native.NATIVE_GAP_S", 0):
        assert await hass.config_entries.async_setup(entry.entry_id)
        scheduler = entry.runtime_data.scheduler
        hass.states.async_set("switch.front_zone", "on")
        await hass.async_block_till_done()
        assert set(scheduler._native_session["zones"]) == {"front"}
        hass.states.async_set("switch.front_zone", "off")
        await hass.async_block_till_done()
    rec = scheduler.records["last_run"]["attributes"]
    assert rec["trigger"] == "rachio" and rec["watered"] == ["front"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.states.async_set("switch.front_zone", "on")
    await hass.async_block_till_done()
    assert scheduler._native_session is None


async def test_zone_data_fetch_uses_the_stored_key(
        hass, aioclient_mock, enable_pyscript_and_rachio):
    """The engine's live runtime pull uses the key stored in the entry, read at
    each fetch; with none it raises (the engine falls back to static values)
    and asks for one."""
    from custom_components.geodrops_rachio.rachio_client import (
        RACHIO_BASE, RachioAuthError)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fetch = entry.runtime_data.scheduler._fetch_zone_data_fn

    with pytest.raises(RachioAuthError):
        await fetch()
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()
            ] == ["reauth"]

    hass.config_entries.async_update_entry(entry, data={**entry.data, "api_key": "K1"})
    await hass.async_block_till_done()
    aioclient_mock.get(RACHIO_BASE + "person/info", json={"id": "p1"})
    aioclient_mock.get(RACHIO_BASE + "person/p1", json={"devices": [{"id": "d1"}]})
    aioclient_mock.get(RACHIO_BASE + "device/d1", json={"zones": [
        {"id": "z1", "runtime": 1800, "enabled": True}]})
    runtimes, _depths, _spans = await fetch()
    assert runtimes == {"z1": 30.0}
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer K1"
    # A key change is not a reload: it would cancel a waiting or watering run.
    assert entry.runtime_data.scheduler._fetch_zone_data_fn is fetch


async def test_rejected_key_at_setup_asks_for_a_new_one_without_blocking(
        hass, aioclient_mock, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio.rachio_client import RACHIO_BASE
    aioclient_mock.get(RACHIO_BASE + "person/info", status=401)
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "api_key": "old"}, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED        # still waters
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()
            ] == ["reauth"]


async def test_unreachable_rachio_at_setup_does_not_ask_for_a_key(
        hass, aioclient_mock, enable_pyscript_and_rachio):
    import asyncio
    from custom_components.geodrops_rachio.rachio_client import RACHIO_BASE
    aioclient_mock.get(RACHIO_BASE + "person/info", exc=asyncio.TimeoutError())
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "api_key": "k"}, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.config_entries.flow.async_progress() == []


async def test_migration_moves_the_key_out_of_secrets_yaml(
        hass, tmp_path, enable_pyscript_and_rachio):
    (tmp_path / "secrets.yaml").write_text("lawn_key: K1\n", encoding="utf-8")
    entry = MockConfigEntry(domain=DOMAIN, minor_version=1, data={
        **DATA, "bindings": {"rachio_api_key_secret": "lawn_key",
                             "notify_service": "notify.phone"}})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio._async_check_api_key", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert (entry.version, entry.minor_version) == (1, 4)
    assert entry.data["api_key"] == "K1"
    # Kept for a rollback to a release that still reads it.
    assert entry.data["bindings"] == {"rachio_api_key_secret": "lawn_key",
                                      "notify_service": "notify.phone"}


async def test_migration_without_a_readable_key_asks_for_one(
        hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, minor_version=1, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 4 and entry.data["api_key"] == ""
    assert entry.state is ConfigEntryState.LOADED
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()
            ] == ["reauth"]


async def test_entry_from_a_newer_major_version_is_refused(
        hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=DATA)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_migration_moves_the_settings_into_options(
        hass, enable_pyscript_and_rachio):
    """1.3 keeps the settings in the options; the data keeps its copies for a
    rollback, and the scheduler runs on the options."""
    entry = MockConfigEntry(domain=DOMAIN, minor_version=2, data={
        **DATA, "api_key": "k", "self_calibration_enabled": True,
        "advanced_overrides": "rain_skip_probability_pct: 80"})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio._async_check_api_key", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.minor_version == 4
    assert entry.options == {"self_calibration_enabled": True,
                             "advanced_overrides": "rain_skip_probability_pct: 80"}
    assert entry.data["self_calibration_enabled"] is True
    tun = entry.runtime_data.scheduler._load_cfg().tunables
    assert tun.self_calibration_enabled is True
    assert tun.rain_skip_probability_pct == 80


async def test_settings_are_read_from_the_options(hass, enable_pyscript_and_rachio):
    """An option changed after the migration wins over the data's stale copy."""
    entry = MockConfigEntry(
        domain=DOMAIN, minor_version=3,
        data={**DATA, "api_key": "k", "self_calibration_enabled": False},
        options={"self_calibration_enabled": True, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio._async_check_api_key", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.scheduler._load_cfg().tunables.self_calibration_enabled
