from __future__ import annotations
from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT, BinarySensorDeviceClass, BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .engine.store import RUN_ACTIVE
from .entity_base import device_info


class RunActiveSensor(BinarySensorEntity):
    """Read-only: on while a collapsed run is in flight. Mirrors the engine's
    persisted run marker, which only the engine sets."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Run active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, entry: ConfigEntry, scheduler) -> None:
        self._attr_unique_id = f"{entry.entry_id}_run_active"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_run_active")
        self._attr_device_info = device_info(entry)
        self._scheduler = scheduler

    async def async_added_to_hass(self) -> None:
        @callback
        def _update() -> None:
            self._attr_is_on = bool(self._scheduler.store.read(RUN_ACTIVE))
            self.async_write_ha_state()
        self.async_on_remove(self._scheduler.add_listener(_update))
        _update()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    async_add_entities([RunActiveSensor(entry, scheduler)])
