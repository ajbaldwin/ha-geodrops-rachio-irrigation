"""Repair issues: what the scheduler depends on that is broken and silent.

Each one is a dependency whose failure the scheduler only logs, because a run
must go on without it:

- missing_entities: a bound entity no longer exists. The scheduler reads it as
  "off" or "no reading": a deleted zone valve switch waters nothing, a deleted
  moisture sensor leaves the zone unplanned.
- unavailable_entities: a bound entity has been unavailable for a day. Same
  effect as missing, but its integration is down rather than removed.
- rachio_not_loaded: no Rachio config entry is loaded, so rachio.stop_watering
  (every safety stop, pause and resume) fails.
- rachio_device_unknown: the bound controller name matches no controller on
  the account. rachio.stop_watering then stops nothing, without an error.
- notify_missing: the bound notify service does not exist; run recaps are lost.
- unload_failed: the scheduler stopped but the entry could not unload, so
  nothing waters until Home Assistant restarts.
- legacy_script_loaded: the v0.9 pyscript app may still be loaded alongside
  the native engine and water a second time tonight.

The entry-scoped checks run once Home Assistant has started (other
integrations' entities and services load after ours) and again on whatever
event can change their answer; unloading the entry clears them, and the next
setup re-raises any that still hold. unload_failed and legacy_script_loaded
are not persistent, so the restart that fixes them also clears them.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange)
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED, EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED,
    STATE_UNAVAILABLE)
from homeassistant.core import (
    CALLBACK_TYPE, CoreState, Event, EventStateChangedData, HomeAssistant,
    callback)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity_renames import bound_entity_ids
from .owned_entities import owned_ids

RACHIO_DOMAIN = "rachio"
NOTIFY_DOMAIN = "notify"
# How long a bound entity may stay unavailable before it is reported: long
# enough that an integration restarting, a cloud outage or a sensor slow to
# come back after a Home Assistant restart never raises it.
UNAVAILABLE_GRACE = dt.timedelta(hours=24)
UNAVAILABLE_CHECK_INTERVAL = dt.timedelta(hours=1)

MISSING_ENTITIES = "missing_entities"
UNAVAILABLE_ENTITIES = "unavailable_entities"
RACHIO_NOT_LOADED = "rachio_not_loaded"
RACHIO_DEVICE_UNKNOWN = "rachio_device_unknown"
NOTIFY_MISSING = "notify_missing"
UNLOAD_FAILED = "unload_failed"
LEGACY_SCRIPT_LOADED = "legacy_script_loaded"
# Cleared on unload; re-raised by the next setup if still true.
_ENTRY_ISSUES = (MISSING_ENTITIES, UNAVAILABLE_ENTITIES, RACHIO_NOT_LOADED,
                 RACHIO_DEVICE_UNKNOWN, NOTIFY_MISSING)


def issue_id(key: str, entry: ConfigEntry) -> str:
    return f"{key}_{entry.entry_id}"


def _set(hass: HomeAssistant, entry: ConfigEntry, key: str, raised: bool,
         severity: ir.IssueSeverity = ir.IssueSeverity.ERROR,
         **placeholders: str) -> None:
    """Raise (or update) the entry's `key` issue, or clear it."""
    if not raised:
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry))
        return
    ir.async_create_issue(
        hass, DOMAIN, issue_id(key, entry),
        is_fixable=False, is_persistent=False, severity=severity,
        translation_key=key,
        translation_placeholders={"title": entry.title, **placeholders})


def _code_list(ids: Iterable[str]) -> str:
    return ", ".join(f"`{eid}`" for eid in ids)


def _bindings(entry: ConfigEntry) -> Mapping[str, Any]:
    return entry.data.get("bindings") or {}


# --- bound entities -----------------------------------------------------------
def _prefixes(value: Any) -> set[str]:
    """The precipitation sensor-name prefixes: entity-id-shaped, not ids."""
    if isinstance(value, Mapping):
        return {item for key, item in value.items()
                if key.endswith("_prefix") and isinstance(item, str)}.union(
                    *(_prefixes(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_prefixes(item) for item in value))
    return set()


def _watched(entry: ConfigEntry) -> set[str]:
    """Bound entity ids the scheduler reads that belong to other integrations."""
    skip = set(owned_ids(entry)) | _prefixes(entry.data)
    return bound_entity_ids(entry.data) - skip


def _missing(hass: HomeAssistant, entry: ConfigEntry,
             deleted: str | None = None) -> list[str]:
    """Bound entity ids with neither a state nor a registry entry. An entity
    whose integration is down still has its registry entry: unavailable, not
    missing. `deleted` was just removed from the registry and counts as
    missing even while its state lingers."""
    reg = er.async_get(hass)
    return sorted(
        eid for eid in _watched(entry)
        if eid == deleted
        or (hass.states.get(eid) is None and reg.async_get(eid) is None))


def _unavailable(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Bound entity ids unavailable for at least UNAVAILABLE_GRACE."""
    cutoff = dt_util.utcnow() - UNAVAILABLE_GRACE
    found = []
    for eid in _watched(entry):
        state = hass.states.get(eid)
        if (state is not None and state.state == STATE_UNAVAILABLE
                and state.last_changed <= cutoff):
            found.append(eid)
    return sorted(found)


@callback
def async_check_missing(hass: HomeAssistant, entry: ConfigEntry,
                        deleted: str | None = None) -> None:
    """Raise, update or clear the entry's missing-entities issue."""
    missing = _missing(hass, entry, deleted)
    _set(hass, entry, MISSING_ENTITIES, bool(missing),
         entities=_code_list(missing))


@callback
def async_check_unavailable(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise, update or clear the entry's unavailable-entities issue."""
    found = _unavailable(hass, entry)
    _set(hass, entry, UNAVAILABLE_ENTITIES, bool(found),
         ir.IssueSeverity.WARNING, entities=_code_list(found))


# --- services -----------------------------------------------------------------
@callback
def async_check_rachio(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The Rachio integration's services work only while one of its entries is
    loaded; they are registered (and stay) whenever it is set up at all."""
    loaded = bool(hass.config_entries.async_loaded_entries(RACHIO_DOMAIN))
    _set(hass, entry, RACHIO_NOT_LOADED, not loaded)


def _notify_service(entry: ConfigEntry) -> str | None:
    service = _bindings(entry).get("notify_service")
    return service if isinstance(service, str) and service else None


@callback
def async_check_notify(hass: HomeAssistant, entry: ConfigEntry) -> None:
    service = _notify_service(entry)
    missing = service is not None and not hass.services.has_service(
        NOTIFY_DOMAIN, service.split(".", 1)[-1])
    _set(hass, entry, NOTIFY_MISSING, missing, ir.IssueSeverity.WARNING,
         service=f"`{service}`")


@callback
def async_check_rachio_device(hass: HomeAssistant, entry: ConfigEntry,
                              controllers: Iterable[str]) -> None:
    """Report a bound controller name that names none of the account's
    `controllers`. rachio.stop_watering takes the name as one string and stops
    each controller whose name is IN it, so a stale name stops nothing."""
    names = [name for name in controllers if name]
    bound = _bindings(entry).get("rachio_device_name") or ""
    _set(hass, entry, RACHIO_DEVICE_UNKNOWN,
         not any(name in bound for name in names),
         device=f"`{bound}`", controllers=", ".join(names) or "none")


# --- lifecycle ----------------------------------------------------------------
@callback
def async_track(hass: HomeAssistant, entry: ConfigEntry) -> CALLBACK_TYPE:
    """Run the entry-scoped checks once Home Assistant has started, then on
    every event that can change one. Returns the unsubscribe callback."""
    unsubs: list[CALLBACK_TYPE] = []
    watched = _watched(entry)

    @callback
    def _check_all() -> None:
        async_check_missing(hass, entry)
        async_check_unavailable(hass, entry)
        async_check_rachio(hass, entry)
        async_check_notify(hass, entry)

    @callback
    def _is_bound_removal(event_data: er.EventEntityRegistryUpdatedData) -> bool:
        return (event_data["action"] == "remove"
                and event_data["entity_id"] in bound_entity_ids(entry.data))

    @callback
    def _on_removal(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        async_check_missing(hass, entry, deleted=event.data["entity_id"])

    @callback
    def _on_bound_state(_event: Event[EventStateChangedData]) -> None:
        async_check_unavailable(hass, entry)

    @callback
    def _on_interval(_now: dt.datetime) -> None:
        async_check_unavailable(hass, entry)

    @callback
    def _is_notify_service(event_data: Mapping[str, Any]) -> bool:
        return event_data.get("domain") == NOTIFY_DOMAIN

    @callback
    def _on_notify_service(_event: Event) -> None:
        async_check_notify(hass, entry)

    @callback
    def _on_entry_changed(_change: ConfigEntryChange,
                          changed: ConfigEntry) -> None:
        if changed.domain == RACHIO_DOMAIN:
            async_check_rachio(hass, entry)

    @callback
    def _start_tracking() -> None:
        _check_all()
        unsubs.extend([
            async_track_state_change_event(hass, list(watched), _on_bound_state),
            async_track_time_interval(
                hass, _on_interval, UNAVAILABLE_CHECK_INTERVAL,
                cancel_on_shutdown=True),
            hass.bus.async_listen(EVENT_SERVICE_REGISTERED, _on_notify_service,
                                  event_filter=_is_notify_service),
            hass.bus.async_listen(EVENT_SERVICE_REMOVED, _on_notify_service,
                                  event_filter=_is_notify_service),
            async_dispatcher_connect(
                hass, SIGNAL_CONFIG_ENTRY_CHANGED, _on_entry_changed),
        ])

    unsubs.append(hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _on_removal,
        event_filter=_is_bound_removal))

    if hass.state is CoreState.running:
        _start_tracking()
    else:
        @callback
        def _on_started(_event: Event) -> None:
            unsubs.remove(unsub_started)
            _start_tracking()

        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_started)
        unsubs.append(unsub_started)

    @callback
    def _unsub() -> None:
        for unsub in unsubs:
            unsub()
        unsubs.clear()

    return _unsub


@callback
def async_clear(hass: HomeAssistant, entry: ConfigEntry) -> None:
    for key in _ENTRY_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry))


@callback
def async_unload_failed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The scheduler stopped but the platforms stayed: nothing waters until
    Home Assistant restarts (a failed unload blocks a reload)."""
    _set(hass, entry, UNLOAD_FAILED, True)


@callback
def async_clear_unload_failed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, issue_id(UNLOAD_FAILED, entry))


@callback
def async_legacy_script_loaded(hass: HomeAssistant, error: str) -> None:
    """pyscript.reload failed after the legacy script's files were removed, so
    it may still be loaded this boot. One per Home Assistant, not per entry:
    pyscript is."""
    ir.async_create_issue(
        hass, DOMAIN, LEGACY_SCRIPT_LOADED,
        is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=LEGACY_SCRIPT_LOADED,
        translation_placeholders={"error": error})

