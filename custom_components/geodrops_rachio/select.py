from __future__ import annotations
from homeassistant.components.select import SelectEntity, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DROUGHT_LEVELS, DEFAULT_DROUGHT_LEVEL
from .entity_base import device_info


class DroughtLevelSelect(RestoreEntity, SelectEntity):
    _attr_should_poll = False
    _attr_options = DROUGHT_LEVELS

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_drought_level"
        self._attr_name = "Drought level"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_drought_level")
        self._attr_current_option = DEFAULT_DROUGHT_LEVEL
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in DROUGHT_LEVELS:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DroughtLevelSelect(entry)])
