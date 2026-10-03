import asyncio
import copy
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import config_writer, entity_renames, issues, owned_entities, rachio_client
from .const import DOMAIN, PLATFORMS
from .coordinator import (
    GeodropsRachioConfigEntry, GeodropsRachioData, ZoneStateCoordinator)
from .engine.port import HassPort
from .engine.scheduler import Scheduler
from .engine.store import async_open_store, async_remove_store
from .entity import device_info
from .util import slug

_LOGGER = logging.getLogger(__name__)

# The secrets.yaml name entries before 1.2 read the Rachio API key from when
# they did not name one.
_LEGACY_SECRET_NAME = "rachio_api_key"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # For the whole integration, so a rename that lands while an entry reloads
    # is still followed (see entity_renames.async_track_renames).
    entity_renames.async_track_renames(
        hass, lambda entry, old, new: _on_rename(hass, entry, old, new))
    return True


async def async_setup_entry(hass: HomeAssistant,
                            entry: GeodropsRachioConfigEntry) -> bool:
    data = config_writer.entry_config(entry.data, entry.options)
    try:
        config_writer.build_config(data)  # validates advanced_overrides
    except ValueError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="invalid_overrides",
            translation_placeholders={"error": str(err)}) from err

    store = await async_open_store(hass, entry.entry_id)
    session = async_get_clientsession(hass)

    async def fetch_zone_data() -> tuple[
            dict[str, float], dict[str, float], dict[str, float]]:
        # Read at each fetch, so a key replaced by reauth applies without a
        # reload (which would cancel a waiting or watering run).
        key = entry.data.get(CONF_API_KEY)
        try:
            if not key:
                raise rachio_client.RachioAuthError("no Rachio API key is set")
            return await rachio_client.async_fetch_zone_data(session, key)
        except rachio_client.RachioAuthError:
            entry.async_start_reauth(hass)
            raise

    # {original id: current id} of bound entities renamed while this entry runs;
    # the engine reads through it until the reload that rebinds them.
    renamed: dict[str, str] = {}

    def load_config() -> dict[str, Any]:
        # Zones' moisture targets come from the entry as it is now: changing
        # one (its select, or the options flow) applies without a reload, which
        # would cancel a waiting or watering run. See _snapshot.
        return config_writer.build_config(
            config_writer.with_targets(data, entry.data.get("zones", [])))

    scheduler = Scheduler(
        HassPort(hass, owned_entities.resolver(hass, entry, renamed)), store,
        load_config,
        fetch_zone_data,
        lambda coro, name: entry.async_create_background_task(hass, coro, name))
    coordinator = ZoneStateCoordinator(hass, entry, scheduler)
    coordinator.async_start()
    # Registered up front so zone devices can link to it by its registry id.
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **device_info(entry))
    entry.runtime_data = GeodropsRachioData(
        data=data, coordinator=coordinator, scheduler=scheduler,
        hub_device_id=hub.id, renamed=renamed, setup_snapshot=_snapshot(entry),
        targets=config_writer.zone_targets(entry.data.get("zones", [])))
    entry.async_on_unload(coordinator.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # A stage-1 shutdown job runs BEFORE HA cancels background tasks (the run
    # task is one) and fires EVENT_HOMEASSISTANT_STOP, so the safety stop still
    # sees a watering run. Removing it after stop is harmless, unlike a spent
    # listen_once listener.
    entry.async_on_unload(
        hass.async_add_shutdown_job(HassJob(scheduler.async_shutdown)))
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    entry.async_on_unload(issues.async_track_missing(hass, entry))
    # Re-raised by the next setup if still true; a disabled or deleted entry
    # must not leave it behind.
    entry.async_on_unload(lambda: issues.async_clear(hass, entry))
    entry.async_create_background_task(
        hass, _async_check_api_key(hass, entry, session), "geodrops_rachio_check_key")
    _purge_orphan_zone_devices(hass, entry)
    _purge_retired_entities(hass, entry)
    # Last, so a failure above cannot leak the scheduler's time triggers.
    scheduler.async_start(hass)
    return True


async def _async_check_api_key(hass: HomeAssistant, entry: ConfigEntry,
                               session: aiohttp.ClientSession) -> None:
    """Ask for a new key at once if Rachio rejects the stored one.

    Setup does not wait for this: the scheduler waters from the zones' stored
    runtimes, and the key only refreshes them, so a rejected key or an
    unreachable Rachio cloud must not stop a night's watering."""
    key = entry.data.get(CONF_API_KEY)
    try:
        if not key:
            raise rachio_client.RachioAuthError("no Rachio API key is set")
        await rachio_client.async_fetch_account(session, key)
    except rachio_client.RachioAuthError:
        entry.async_start_reauth(hass)
    except rachio_client.RachioConnectionError as err:
        _LOGGER.info("Rachio is not reachable (%s); zones keep their stored "
                     "runtimes until it is", err)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 named a secrets.yaml entry holding the Rachio API key; 1.2 stores the
    key in the entry. A key that cannot be read is left empty and setup asks
    for it (reauth). 1.3 keeps the settings (const.SETTINGS_KEYS) in the
    entry's options rather than its data. 1.4 drops the bindings of the
    retired switches.

    Minor versions, so each change stays loadable by older releases (Home
    Assistant loads an entry with a newer minor version of the same major): a
    rollback keeps the entry and its calibration history. For the same reason
    the secrets.yaml name stays in the bindings, and the settings' copies in
    the data, unused here (a rollback reads the settings as they were when
    migrated)."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        data = copy.deepcopy(dict(entry.data))
        bindings = data.get("bindings", {})
        name = bindings.get("rachio_api_key_secret") or _LEGACY_SECRET_NAME
        data[CONF_API_KEY] = await rachio_client.resolve_secret(hass, name) or ""
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
        if data[CONF_API_KEY]:
            _LOGGER.info("Moved the Rachio API key from secrets.yaml (%s) into "
                         "the integration's settings", name)
        else:
            _LOGGER.warning("No Rachio API key found in secrets.yaml (%s); "
                            "Home Assistant will ask for it", name)
    if entry.minor_version < 3:
        _data, settings = config_writer.split_settings(
            {"self_calibration_enabled": False, "advanced_overrides": "",
             **entry.data})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **settings}, minor_version=3)
    if entry.minor_version < 4:
        # Entries made before 1.1 bind the retired switches (see
        # _RETIRED_ENTITIES); setup deletes those, so the missing-entities
        # check would report the bindings. Nothing since 1.1 reads them.
        data = copy.deepcopy(dict(entry.data))
        bindings = data.get("bindings", {})
        for key in _RETIRED_BINDINGS:
            bindings.pop(key, None)
        hass.config_entries.async_update_entry(entry, data=data, minor_version=4)
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
# The bindings that pointed the engine at them.
_RETIRED_BINDINGS = ("dew_formed_boolean", "run_active_boolean")


def _purge_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    reg = er.async_get(hass)
    for platform, suffix in _RETIRED_ENTITIES:
        entity_id = reg.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{suffix}")
        if entity_id is not None:
            reg.async_remove(entity_id)


def _snapshot(entry: ConfigEntry) -> dict[str, Any]:
    # Without the API key: the running scheduler reads it from the entry on each
    # fetch, so replacing it needs no reload. Without the zones' moisture
    # targets for the same reason: it reads them at each config load.
    data = {k: v for k, v in entry.data.items() if k != CONF_API_KEY}
    if "zones" in data:
        data["zones"] = [{k: v for k, v in z.items() if k != "target_range"}
                         for z in data["zones"]]
    return {"data": copy.deepcopy(data),
            "options": copy.deepcopy(dict(entry.options))}


async def async_reload_if_changed(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload the entry only if its data or options differ from what the
    running entry was set up with (v0.9.x likewise only touched the run when the
    config actually changed). A reload cancels a waiting or watering run, so an
    options "Done" with nothing edited must not trigger one. An entry that is
    not running always reloads — including one whose snapshot outlived it (a
    failed platform unload, or setup failing after the snapshot was stored).
    Returns whether it reloaded."""
    runtime = _runtime(entry)
    if (entry.state is ConfigEntryState.LOADED and runtime is not None
            and runtime.setup_snapshot == _snapshot(entry)):
        _LOGGER.debug("configuration unchanged; not reloading")
        await _apply_targets(entry, runtime)
        return False
    await hass.config_entries.async_reload(entry.entry_id)
    return True


async def _apply_targets(entry: ConfigEntry, runtime: GeodropsRachioData) -> None:
    """A changed moisture target applies without a reload (the engine reads
    targets at each config load); republish the target floors so the Deficit
    sensors measure against it now, not from the next plan."""
    targets = config_writer.zone_targets(entry.data.get("zones", []))
    if targets != runtime.targets:
        runtime.targets = targets
        await runtime.scheduler.async_publish_targets()


def _runtime(entry: ConfigEntry) -> GeodropsRachioData | None:
    """The entry's runtime data, or None while it is not set up."""
    return getattr(entry, "runtime_data", None)


@callback
def _on_rename(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
               old: str, new: str) -> None:
    """A bound entity was renamed; entity_renames rewrites the entry next."""
    runtime = _runtime(entry)
    if runtime is None:
        return
    entity_renames.record_rename(runtime.renamed, old, new)
    runtime.rename_pending = True
    _LOGGER.info("Following the rename of %s to %s", old, new)


def _run_in_flight(runtime: GeodropsRachioData) -> bool:
    task = runtime.scheduler.run_task
    return task is not None and not task.done()


@callback
def _reload_when_run_ends(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                          runtime: GeodropsRachioData) -> None:
    if runtime.reload_after_run:
        return
    runtime.reload_after_run = True

    @callback
    def _run_ended(_task: asyncio.Task[Any]) -> None:
        # Only for the entry instance that deferred it: a reload in between has
        # already rebound the renamed entities.
        if (_runtime(entry) is runtime
                and entry.state is ConfigEntryState.LOADED):
            hass.async_create_task(async_reload_if_changed(hass, entry),
                                   "geodrops_rachio_reload_after_rename")

    task = runtime.scheduler.run_task
    assert task is not None  # the caller checked _run_in_flight
    task.add_done_callback(_run_ended)


async def _reload_on_options(hass: HomeAssistant,
                             entry: GeodropsRachioConfigEntry) -> None:
    # An options flow persists each edit via async_update_entry, which fires
    # this listener. While the dialog is open we defer the scheduler restart:
    # the options flow sets suppress_reload and, on "Done", clears it and fires
    # at most one reload. See config_flow.GeodropsRachioOptionsFlow.
    runtime = _runtime(entry)
    if runtime is None:
        await async_reload_if_changed(hass, entry)
        return
    rename, runtime.rename_pending = runtime.rename_pending, False
    if runtime.suppress_reload:
        return
    if rename and _run_in_flight(runtime):
        # A reload would cancel the waiting or watering run. The engine already
        # reads the new id (the resolver's rename map), so rebind once it ends.
        _reload_when_run_ends(hass, entry, runtime)
        return
    await async_reload_if_changed(hass, entry)


async def async_unload_entry(hass: HomeAssistant,
                             entry: GeodropsRachioConfigEntry) -> bool:
    await entry.runtime_data.scheduler.async_shutdown()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not ok:
        _LOGGER.warning(
            "platforms did not unload; the scheduler is already stopped, so no nightly "
            "run will fire until Home Assistant restarts")
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # The calibration history and run records live only in this entry's Store.
    await async_remove_store(hass, entry.entry_id)
