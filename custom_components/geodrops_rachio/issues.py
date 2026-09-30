"""Repair issue for entities picked in setup that no longer exist.

The scheduler reads a missing entity as "off" or "no reading": a deleted
zone valve switch waters nothing, a deleted moisture sensor leaves the zone
unplanned, and nothing else says so. Checked once Home Assistant has started
(other integrations' entities load after ours) and again whenever a bound
entity is deleted; a reload after Configure re-checks and clears the issue.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import (
    CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .entity_renames import bound_entity_ids
from .owned_entities import owned_ids


def _issue_id(entry: ConfigEntry) -> str:
    return f"missing_entities_{entry.entry_id}"


def _prefixes(value: Any) -> set[str]:
    """The precipitation sensor-name prefixes: entity-id-shaped, not ids."""
    if isinstance(value, Mapping):
        return {item for key, item in value.items()
                if key.endswith("_prefix") and isinstance(item, str)}.union(
                    *(_prefixes(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_prefixes(item) for item in value))
    return set()


def _missing(hass: HomeAssistant, entry: ConfigEntry,
             deleted: str | None = None) -> list[str]:
    """Bound entity ids with neither a state nor a registry entry. An entity
    whose integration is down still has its registry entry: unavailable, not
    missing. `deleted` was just removed from the registry and counts as
    missing even while its state lingers."""
    reg = er.async_get(hass)
    skip = set(owned_ids(entry)) | _prefixes(entry.data)
    return sorted(
        eid for eid in bound_entity_ids(entry.data) - skip
        if eid == deleted
        or (hass.states.get(eid) is None and reg.async_get(eid) is None))


@callback
def async_check_missing(hass: HomeAssistant, entry: ConfigEntry,
                        deleted: str | None = None) -> None:
    """Raise, update or clear the entry's missing-entities issue."""
    missing = _missing(hass, entry, deleted)
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
        return
    ir.async_create_issue(
        hass, DOMAIN, _issue_id(entry),
        is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="missing_entities",
        translation_placeholders={
            "title": entry.title,
            "entities": ", ".join(f"`{eid}`" for eid in missing)})


@callback
def async_track_missing(hass: HomeAssistant, entry: ConfigEntry) -> CALLBACK_TYPE:
    """Check now (or once Home Assistant has started) and on every deletion
    of a bound entity. Returns the unsubscribe callback."""
    unsubs: list[CALLBACK_TYPE] = []

    @callback
    def _is_bound_removal(event_data: er.EventEntityRegistryUpdatedData) -> bool:
        return (event_data["action"] == "remove"
                and event_data["entity_id"] in bound_entity_ids(entry.data))

    @callback
    def _on_removal(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        async_check_missing(hass, entry, deleted=event.data["entity_id"])

    unsubs.append(hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _on_removal,
        event_filter=_is_bound_removal))

    if hass.state is CoreState.running:
        async_check_missing(hass, entry)
    else:
        @callback
        def _on_started(_event: Event) -> None:
            unsubs.remove(unsub_started)
            async_check_missing(hass, entry)

        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_started)
        unsubs.append(unsub_started)

    @callback
    def _unsub() -> None:
        for unsub in unsubs:
            unsub()

    return _unsub


@callback
def async_clear(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
