from __future__ import annotations
import copy
from typing import Any
from homeassistant.components.select import SelectEntity, ENTITY_ID_FORMAT
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .brain.config import BAND_ORDER
from .const import DROUGHT_LEVELS, DEFAULT_DROUGHT_LEVEL
from .coordinator import GeodropsRachioConfigEntry
from .entity import GeodropsRachioEntity, GeodropsRachioZoneEntity

# A local setting; nothing to fetch.
PARALLEL_UPDATES = 0


class DroughtLevelSelect(GeodropsRachioEntity, RestoreEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = DROUGHT_LEVELS

    def __init__(self, entry: GeodropsRachioConfigEntry) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "drought_level")
        self._attr_current_option = DEFAULT_DROUGHT_LEVEL
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in DROUGHT_LEVELS:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
        # The level shifts every zone's target floor; republish them so the
        # Deficit sensors follow now, not from the next plan. After the state
        # write: the engine reads the level from this entity's state.
        await self._entry.runtime_data.scheduler.async_publish_targets()


class ZoneMoistureTargetSelect(GeodropsRachioZoneEntity, SelectEntity):
    """The GeoDrops moisture band the scheduler keeps the zone at: the zone's
    target_range, the same setting the options flow edits. Kept in the entry,
    so the two never disagree; a change applies from the next plan without
    restarting the scheduler (see __init__._snapshot)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = BAND_ORDER

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id,
                         "moisture_target")
        self._entry = entry

    @property
    def current_option(self) -> str | None:
        zone: dict[str, Any] = next(
            (z for z in self._entry.data.get("zones", [])
             if z["key"] == self._zone_key), {})
        target = zone.get("target_range")
        return target if target in BAND_ORDER else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _entry_updated(_hass: HomeAssistant,
                                 _entry: GeodropsRachioConfigEntry) -> None:
            # The options flow may have changed the target.
            self.async_write_ha_state()

        self.async_on_remove(self._entry.add_update_listener(_entry_updated))

    async def async_select_option(self, option: str) -> None:
        data = copy.deepcopy(dict(self._entry.data))
        for zone in data.get("zones", []):
            if zone["key"] == self._zone_key:
                zone["target_range"] = option
        self.hass.config_entries.async_update_entry(self._entry, data=data)
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    hub_device_id = entry.runtime_data.hub_device_id
    entities: list[SelectEntity] = [DroughtLevelSelect(entry)]
    entities.extend(
        ZoneMoistureTargetSelect(entry, z["key"], hub_device_id)
        for z in entry.data.get("zones", []))
    async_add_entities(entities)
