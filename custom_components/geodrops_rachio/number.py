from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from homeassistant.components.number import (
    ENTITY_ID_FORMAT, NumberExtraStoredData, NumberMode, RestoreNumber)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import restore_state
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .brain.config import FINISH_SHIFT_MAX
from .const import DEFAULT_DROUGHT_LEVEL, DROUGHT_LEVELS
from .coordinator import GeodropsRachioConfigEntry
from .entity import GeodropsRachioEntity

# A local setting; nothing to fetch.
PARALLEL_UPDATES = 0

DROUGHT_LEVEL_ENTITY = "select.geodrops_rachio_drought_level"


def level_changed_signal(entry_id: str) -> str:
    """Dispatcher signal the drought level select sends when the user picks a
    different level."""
    return f"geodrops_rachio_{entry_id}_drought_level_changed"


@dataclass
class _FinishExtraStoredData(NumberExtraStoredData):
    """The number's restore data, marked as holding the actual finish: values
    saved before that were shifts of the level's own end."""

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "from_anchor": True}


class FinishOffsetNumber(GeodropsRachioEntity, RestoreNumber):
    """When watering finishes, in minutes after the end anchor: positive is
    later, negative earlier, 0 is the anchor itself. Starts at the drought
    level's own finish and returns to it whenever the level changes; a value
    set by hand holds until then. Read by the engine from this entity's state
    at each plan, so a change needs no reload (see
    brain.config.finish_end_offset)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = -FINISH_SHIFT_MAX
    _attr_native_max_value = FINISH_SHIFT_MAX
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, entry: GeodropsRachioConfigEntry) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "finish_offset")
        self._entry = entry
        self._attr_native_value = 0

    @property
    def extra_restore_state_data(self) -> NumberExtraStoredData:
        data = super().extra_restore_state_data
        return _FinishExtraStoredData(
            data.native_max_value, data.native_min_value, data.native_step,
            data.native_unit_of_measurement, data.native_value)

    def _level_finish(self, level: str) -> int | None:
        return self._entry.runtime_data.scheduler.level_finish_offset(level)

    def _current_level(self) -> str:
        """The drought level now in force. The select may not be added yet, so
        fall back to the state it will restore."""
        state = self.hass.states.get(DROUGHT_LEVEL_ENTITY)
        if state is None:
            state = restore_state.async_get(self.hass).last_states.get(
                DROUGHT_LEVEL_ENTITY)
            state = state.state if state is not None else None
        else:
            state = state.state
        return state if state in DROUGHT_LEVELS else DEFAULT_DROUGHT_LEVEL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, level_changed_signal(self._entry.entry_id),
            self._level_changed))
        last = await self.async_get_last_number_data()
        level_finish = self._level_finish(self._current_level())
        if last is not None and last.native_value is not None:
            value = last.native_value
            extra = await self.async_get_last_extra_data()
            if not (extra and extra.as_dict().get("from_anchor")):
                # An older save: a shift of the level's own end.
                value += level_finish or 0
        elif level_finish is not None:
            value = level_finish
        else:
            return
        self._attr_native_value = max(
            -FINISH_SHIFT_MAX, min(FINISH_SHIFT_MAX, value))

    @callback
    def _level_changed(self, level: str) -> None:
        finish = self._level_finish(level)
        if finish is not None:
            self._attr_native_value = finish
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([FinishOffsetNumber(entry)])
