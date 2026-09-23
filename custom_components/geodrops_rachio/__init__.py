import copy
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import config_writer, rachio_client
from .const import DOMAIN, PLATFORMS
from .coordinator import ZoneStateCoordinator
from .engine.port import HassPort
from .engine.scheduler import Scheduler
from .engine.store import async_open_store
from .util import slug

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = dict(entry.data)
    try:
        config_writer.build_config(data)  # validates advanced_overrides
    except ValueError as err:
        raise ConfigEntryNotReady(str(err)) from err

    store = await async_open_store(hass, entry.entry_id)
    session = async_get_clientsession(hass)

    async def fetch_zone_data(key_name: str):
        key = await rachio_client.resolve_secret(hass, key_name)
        if not key:
            _LOGGER.warning(
                "irrigation: no %s in secrets.yaml; using static values", key_name)
            return {}, {}, {}
        return await rachio_client.async_fetch_zone_data(session, key)

    scheduler = Scheduler(
        HassPort(hass), store, lambda: config_writer.build_config(data),
        fetch_zone_data,
        lambda coro, name: entry.async_create_background_task(hass, coro, name))
    coordinator = ZoneStateCoordinator(hass, entry, scheduler)
    coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "data": data, "coordinator": coordinator, "scheduler": scheduler,
        # What this running entry was set up with; a reload that would not
        # change it is skipped (see async_reload_if_changed).
        "setup_snapshot": _snapshot(entry)}
    entry.async_on_unload(coordinator.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # A stage-1 shutdown job runs BEFORE HA cancels background tasks (the run
    # task is one) and fires EVENT_HOMEASSISTANT_STOP, so the safety stop still
    # sees a watering run. Removing it after stop is harmless, unlike a spent
    # listen_once listener.
    entry.async_on_unload(
        hass.async_add_shutdown_job(HassJob(scheduler.async_shutdown)))
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    _purge_orphan_zone_devices(hass, entry)
    # Last, so a failure above cannot leak the scheduler's time triggers.
    scheduler.async_start(hass)
    return True


def _purge_orphan_zone_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    reg = dr.async_get(hass)
    keep = {f"{entry.entry_id}:zone:{slug(z['key'])}"
            for z in entry.data.get("zones", [])}
    for device in dr.async_entries_for_config_entry(reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain == DOMAIN and ":zone:" in ident and ident not in keep:
                reg.async_remove_device(device.id)
                break


def _snapshot(entry: ConfigEntry) -> dict:
    return {"data": copy.deepcopy(dict(entry.data)),
            "options": copy.deepcopy(dict(entry.options))}


async def async_reload_if_changed(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload the entry only if its data or options differ from what the
    running entry was set up with (v0.9.x likewise only touched the run when the
    config actually changed). A reload cancels a waiting or watering run, so an
    options "Done" with nothing edited must not trigger one. An entry that is
    not running (no snapshot) always reloads. Returns whether it reloaded."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    snapshot = store.get("setup_snapshot")
    if snapshot is not None and snapshot == _snapshot(entry):
        _LOGGER.debug("geodrops_rachio: configuration unchanged; not reloading")
        return False
    await hass.config_entries.async_reload(entry.entry_id)
    return True


async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # An options flow persists each edit via async_update_entry, which fires
    # this listener. While the dialog is open we defer the scheduler restart:
    # the options flow sets suppress_reload and, on "Done", clears it and fires
    # at most one reload. See config_flow.GeodropsRachioOptionsFlow.
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if store.get("suppress_reload"):
        return
    await async_reload_if_changed(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is not None:
        await stored["scheduler"].async_shutdown()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return ok
