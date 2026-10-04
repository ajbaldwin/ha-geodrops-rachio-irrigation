"""The integration's own entities the engine reads, found by unique_id.

The scheduler config binds these by the entity_id each is created with (e.g.
switch.geodrops_rachio_standby). Home Assistant lets a user change an entity's
id; read by the old id, the entity would look missing, which the engine treats
as "off" — Standby and a zone's Exclude would silently stop applying and the
scheduler would water. `resolver` maps each of these created ids to the
entity's current id through the entity registry, so a rename changes nothing.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .util import slug

_WEATHER_KEYS = ("temp", "humidity", "wind")


def owned_ids(entry: ConfigEntry) -> dict[str, tuple[str, str]]:
    """{created entity_id: (platform domain, unique_id)} for every entity of
    `entry` the engine reads or logs against."""
    eid = entry.entry_id
    out = {
        "select.geodrops_rachio_drought_level": ("select", f"{eid}_drought_level"),
        "select.geodrops_rachio_finish_anchor": ("select", f"{eid}_finish_anchor"),
        "number.geodrops_rachio_finish_offset": ("number", f"{eid}_finish_offset"),
        "switch.geodrops_rachio_standby": ("switch", f"{eid}_standby"),
        "sensor.geodrops_rachio_status": ("sensor", f"{eid}_status"),
    }
    for kind in ("forecast", "observed"):
        for key in _WEATHER_KEYS:
            out[f"sensor.geodrops_rachio_{kind}_overnight_{key}"] = (
                "sensor", f"{eid}_{kind}_overnight_{key}")
    for zone in entry.data.get("zones", []):
        s = slug(zone["key"])
        out[f"switch.geodrops_rachio_{s}_exclude"] = (
            "switch", f"{eid}_zone_{s}_exclude")
    return out


def resolver(hass: HomeAssistant, entry: ConfigEntry,
             renamed: Mapping[str, str] | None = None) -> Callable[[str], str]:
    """A function mapping an entity_id to the id to read now.

    `renamed` ({old id: current id}) carries renames of the entities the user
    bound, seen while this entry runs (see entity_renames); an owned entity's
    current id comes from the registry. Anything else, or an owned entity not
    yet registered, is returned unchanged."""
    owned = owned_ids(entry)
    registry = er.async_get(hass)
    renamed = {} if renamed is None else renamed

    def resolve(entity_id: str) -> str:
        entity_id = renamed.get(entity_id, entity_id)
        target = owned.get(entity_id)
        if target is None:
            return entity_id
        current = registry.async_get_entity_id(target[0], DOMAIN, target[1])
        return current or entity_id

    return resolve
