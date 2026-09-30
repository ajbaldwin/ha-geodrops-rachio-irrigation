from __future__ import annotations
from homeassistant.components.select import SelectEntity, ENTITY_ID_FORMAT
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DROUGHT_LEVELS, DEFAULT_DROUGHT_LEVEL
from .coordinator import GeodropsRachioConfigEntry
from .entity import GeodropsRachioEntity

# A local setting; nothing to fetch.
PARALLEL_UPDATES = 0


class DroughtLevelSelect(GeodropsRachioEntity, RestoreEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = DROUGHT_LEVELS

    def __init__(self, entry: GeodropsRachioConfigEntry) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "drought_level")
        self._attr_current_option = DEFAULT_DROUGHT_LEVEL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in DROUGHT_LEVELS:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DroughtLevelSelect(entry)])
