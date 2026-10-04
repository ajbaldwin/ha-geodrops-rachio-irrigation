from __future__ import annotations
from homeassistant.components.number import (
    ENTITY_ID_FORMAT, NumberMode, RestoreNumber)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .brain.config import FINISH_SHIFT_MAX
from .coordinator import GeodropsRachioConfigEntry
from .entity import GeodropsRachioEntity

# A local setting; nothing to fetch.
PARALLEL_UPDATES = 0


class FinishOffsetNumber(GeodropsRachioEntity, RestoreNumber):
    """Moves the end of the watering window from where the drought level puts
    it: positive finishes later, negative earlier, 0 changes nothing. Read by
    the engine from this entity's state at each plan, so a change needs no
    reload (see brain.config.finish_end_offset)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = -FINISH_SHIFT_MAX
    _attr_native_max_value = FINISH_SHIFT_MAX
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, entry: GeodropsRachioConfigEntry) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "finish_offset")
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = max(
                -FINISH_SHIFT_MAX, min(FINISH_SHIFT_MAX, last.native_value))

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([FinishOffsetNumber(entry)])
