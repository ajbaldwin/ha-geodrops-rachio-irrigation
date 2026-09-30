import copy
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import config_writer, entity_renames, owned_entities, rachio_client
from .const import DOMAIN, PLATFORMS
from .coordinator import ZoneStateCoordinator
from .engine.port import HassPort
from .engine.scheduler import Scheduler
from .engine.store import async_open_store, async_remove_store
from .entity_base import device_info
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
                "no %s in secrets.yaml; using static values", key_name)
            return {}, {}, {}
        return await rachio_client.async_fetch_zone_data(session, key)

    # {original id: current id} of bound entities renamed while this entry runs;
    # the engine reads through it until the reload that rebinds them.
    renamed: dict[str, str] = {}
    scheduler = Scheduler(
        HassPort(hass, owned_entities.resolver(hass, entry, renamed)), store,
        lambda: config_writer.build_config(data),
        fetch_zone_data,
        lambda coro, name: entry.async_create_background_task(hass, coro, name))
    coordinator = ZoneStateCoordinator(hass, entry, scheduler)
    coordinator.async_start()
    # Registered up front so zone devices can link to it by its registry id.
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **device_info(entry))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "data": data, "coordinator": coordinator, "scheduler": scheduler,
        "hub_device_id": hub.id, "renamed": renamed,
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
    entry.async_on_unload(entity_renames.async_track_renames(
        hass, entry, lambda old, new: _on_rename(hass, entry, old, new)))
    _purge_orphan_zone_devices(hass, entry)
    _purge_retired_entities(hass, entry)
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


# (platform, unique-id suffix) of entities a past version created and this one
# no longer does; left in the registry they would linger as unavailable.
_RETIRED_ENTITIES = (("switch", "dew_formed"), ("switch", "run_active"))


def _purge_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    reg = er.async_get(hass)
    for platform, suffix in _RETIRED_ENTITIES:
        entity_id = reg.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{suffix}")
        if entity_id is not None:
            reg.async_remove(entity_id)


def _snapshot(entry: ConfigEntry) -> dict:
    return {"data": copy.deepcopy(dict(entry.data)),
            "options": copy.deepcopy(dict(entry.options))}


async def async_reload_if_changed(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload the entry only if its data or options differ from what the
    running entry was set up with (v0.9.x likewise only touched the run when the
    config actually changed). A reload cancels a waiting or watering run, so an
    options "Done" with nothing edited must not trigger one. An entry that is
    not running always reloads — including one whose snapshot outlived it (a
    failed platform unload, or setup failing after the snapshot was stored).
    Returns whether it reloaded."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    snapshot = store.get("setup_snapshot")
    if (entry.state is ConfigEntryState.LOADED and snapshot is not None
            and snapshot == _snapshot(entry)):
        _LOGGER.debug("configuration unchanged; not reloading")
        return False
    await hass.config_entries.async_reload(entry.entry_id)
    return True


@callback
def _on_rename(hass: HomeAssistant, entry: ConfigEntry, old: str, new: str) -> None:
    """A bound entity was renamed; entity_renames rewrites the entry next."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is None:
        return
    entity_renames.record_rename(store["renamed"], old, new)
    store["rename_pending"] = True
    _LOGGER.info("Following the rename of %s to %s", old, new)


def _run_in_flight(store: dict) -> bool:
    scheduler = store.get("scheduler")
    task = scheduler.run_task if scheduler is not None else None
    return task is not None and not task.done()


@callback
def _reload_when_run_ends(hass: HomeAssistant, entry: ConfigEntry, store: dict) -> None:
    if store.get("reload_after_run"):
        return
    store["reload_after_run"] = True

    @callback
    def _run_ended(_task) -> None:
        # Only for the entry instance that deferred it: a reload in between has
        # already rebound the renamed entities.
        if (hass.data.get(DOMAIN, {}).get(entry.entry_id) is store
                and entry.state is ConfigEntryState.LOADED):
            hass.async_create_task(async_reload_if_changed(hass, entry),
                                   "geodrops_rachio_reload_after_rename")

    store["scheduler"].run_task.add_done_callback(_run_ended)


async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # An options flow persists each edit via async_update_entry, which fires
    # this listener. While the dialog is open we defer the scheduler restart:
    # the options flow sets suppress_reload and, on "Done", clears it and fires
    # at most one reload. See config_flow.GeodropsRachioOptionsFlow.
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    rename = store.pop("rename_pending", False)
    if store.get("suppress_reload"):
        return
    if rename and _run_in_flight(store):
        # A reload would cancel the waiting or watering run. The engine already
        # reads the new id (the resolver's rename map), so rebind once it ends.
        _reload_when_run_ends(hass, entry, store)
        return
    await async_reload_if_changed(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is not None:
        await stored["scheduler"].async_shutdown()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    else:
        _LOGGER.warning(
            "platforms did not unload; the scheduler is already stopped, so no nightly "
            "run will fire until Home Assistant restarts")
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # The calibration history and run records live only in this entry's Store.
    await async_remove_store(hass, entry.entry_id)
