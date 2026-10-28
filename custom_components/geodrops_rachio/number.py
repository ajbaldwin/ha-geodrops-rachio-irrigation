from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from homeassistant.components.number import (
    ENTITY_ID_FORMAT, NumberExtraStoredData, NumberMode, RestoreNumber)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, restore_state
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .brain.config import FINISH_SHIFT_MAX
from .const import (
    DEFAULT_DROUGHT_LEVEL, DOMAIN, DROUGHT_LEVEL_ENTITY, DROUGHT_LEVELS,
    level_changed_signal)
from .coordinator import GeodropsRachioConfigEntry
from .entity import GeodropsRachioEntity

# A local setting; nothing to fetch.
PARALLEL_UPDATES = 0

@dataclass
class _FinishExtraStoredData(NumberExtraStoredData):
    """The number's restore data, marked as holding the actual finish: values
    saved before that were shifts of the level's own end. level_finish is the
    level finish the value was last set from, so an untouched slider can follow
    an edit to it."""

    level_finish: int | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {**super().as_dict(), "from_anchor": True}
        if self.level_finish is not None:
            data["level_finish"] = self.level_finish
        return data


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
        # The level finish the value was last set from; None once unknown.
        self._level_finish_set: int | None = None
        # An old save (a shift) not yet converted, as no level finish was
        # available; kept unmarked so the next start converts it.
        self._unconverted: float | None = None

    @property
    def extra_restore_state_data(self) -> NumberExtraStoredData:
        data = super().extra_restore_state_data
        if self._unconverted is not None:
            return NumberExtraStoredData(
                data.native_max_value, data.native_min_value, data.native_step,
                data.native_unit_of_measurement, self._unconverted)
        return _FinishExtraStoredData(
            data.native_max_value, data.native_min_value, data.native_step,
            data.native_unit_of_measurement, data.native_value,
            self._level_finish_set)

    def _level_finish(self, level: str) -> int | None:
        return self._entry.runtime_data.scheduler.level_finish_offset(level)

    def _current_level(self) -> str:
        """The drought level now in force. The select may not be added yet, so
        fall back to the state it will restore. Its id is looked up in the
        registry, as the user may have renamed it."""
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"{self._entry.entry_id}_drought_level"
        ) or DROUGHT_LEVEL_ENTITY
        state = self.hass.states.get(entity_id)
        if state is None:
            # A StoredState, which wraps the saved State.
            stored = restore_state.async_get(self.hass).last_states.get(entity_id)
            state = stored.state if stored is not None else None
        level = state.state if state is not None else None
        return level if level in DROUGHT_LEVELS else DEFAULT_DROUGHT_LEVEL

    @staticmethod
    def _clamp(value: float) -> float:
        return max(-FINISH_SHIFT_MAX, min(FINISH_SHIFT_MAX, value))

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
            saved = extra.as_dict() if extra else {}
            if saved.get("from_anchor"):
                saved_finish = saved.get("level_finish")
                self._level_finish_set = saved_finish
                if (saved_finish is not None and value == saved_finish
                        and level_finish is not None
                        and level_finish != saved_finish):
                    # Never moved off the level's finish: follow its edit.
                    value = self._level_finish_set = level_finish
            elif level_finish is None:
                # An older save (a shift of the level's own end) with nothing
                # to convert it by: read unknown, so the engine uses the
                # level's own finish, and keep the save for the next start.
                self._unconverted = value
                self._attr_native_value = None
                return
            else:
                value += level_finish
                self._level_finish_set = level_finish
        elif level_finish is not None:
            value = self._level_finish_set = level_finish
        else:
            self._attr_native_value = None
            return
        self._attr_native_value = self._clamp(value)

    @callback
    def _level_changed(self, level: str) -> None:
        finish = self._level_finish(level)
        if finish is not None:
            self._attr_native_value = finish
            self._level_finish_set = finish
            self._unconverted = None
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._unconverted = None
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([FinishOffsetNumber(entry)])
