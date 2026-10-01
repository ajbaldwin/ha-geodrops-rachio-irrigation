"""Follow renames of the entities the user bound in the wizard.

The config entry stores the entity ids picked in the wizard (the Rachio
standby switch, each zone's valve switch and GeoDrops sensors, the weather
sensors, ...). Home Assistant lets a user change an entity's id; read by the
old id the entity would look missing, which the engine treats as "off" — a
renamed Rachio standby switch would stop pausing watering. On a rename this
rewrites the stored id, so the next load binds the new one, and reports the
rename so the running engine reads the new id at once.

The integration's own entities are not rewritten: owned_entities finds those
through the registry by unique_id.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.core import valid_entity_id
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

# Fields whose values are not entity ids even when shaped like one: the notify
# target is a service name (a notify entity can share it), the overrides are
# free YAML text, the API key is a secret.
_NOT_ENTITY_FIELDS = frozenset({"notify_service", "advanced_overrides", "api_key"})


def bound_entity_ids(data: Any) -> set[str]:
    """Every entity id stored in config entry `data`."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        # Mapping, not dict: a config entry's data is a read-only proxy.
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key not in _NOT_ENTITY_FIELDS:
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str) and valid_entity_id(value):
            found.add(value)

    walk(data)
    return found


def replace_entity_id(data: Any, old: str, new: str) -> Any:
    """A copy of `data` with every stored `old` entity id replaced by `new`."""
    if isinstance(data, Mapping):
        return {key: item if key in _NOT_ENTITY_FIELDS
                else replace_entity_id(item, old, new)
                for key, item in data.items()}
    if isinstance(data, (list, tuple)):
        return [replace_entity_id(item, old, new) for item in data]
    return new if data == old else data


def record_rename(renamed: dict[str, str], old: str, new: str) -> None:
    """Fold `old` -> `new` into {original id: current id}, so an id renamed
    twice still maps to where it is now."""
    for original, current in list(renamed.items()):
        if current == old:
            renamed[original] = new
    renamed[old] = new
    renamed.pop(new, None)


def _binding_entries(hass: HomeAssistant, entity_id: str) -> list[ConfigEntry]:
    """This integration's entries that bind `entity_id`, loaded or not."""
    return [entry for entry in hass.config_entries.async_entries(DOMAIN)
            if entity_id in bound_entity_ids(entry.data)]


@callback
def async_track_renames(
        hass: HomeAssistant,
        on_rename: Callable[[ConfigEntry, str, str], None]) -> CALLBACK_TYPE:
    """Rewrite each entry that binds an entity when it is renamed, then call
    on_rename(entry, old, new). Returns the unsubscribe callback.

    One listener for the integration, not one per loaded entry: following a
    rename reloads the entry, and a per-entry listener is gone while it
    reloads. A burst of renames (Home Assistant renames a device's entities
    along with the device) would then follow only the first."""

    @callback
    def _is_bound_rename(event_data: er.EventEntityRegistryUpdatedData) -> bool:
        old = event_data.get("old_entity_id")
        return (event_data["action"] == "update" and old is not None
                and bool(_binding_entries(hass, old)))

    @callback
    def _on_registry_update(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        old, new = event.data["old_entity_id"], event.data["entity_id"]
        reg_entry = er.async_get(hass).async_get(new)
        for entry in _binding_entries(hass, old):
            if reg_entry is not None and reg_entry.config_entry_id == entry.entry_id \
                    and reg_entry.platform == DOMAIN:
                continue
            on_rename(entry, old, new)
            hass.config_entries.async_update_entry(
                entry, data=replace_entity_id(entry.data, old, new))

    return hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_update,
        event_filter=_is_bound_rename)
