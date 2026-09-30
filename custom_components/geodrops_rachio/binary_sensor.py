from __future__ import annotations
from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT, BinarySensorDeviceClass, BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .coordinator import GeodropsRachioConfigEntry
from .engine.scheduler import Scheduler
from .engine.store import RUN_ACTIVE
from .entity import GeodropsRachioEntity

# Pushed by the scheduler; nothing to fetch.
PARALLEL_UPDATES = 0


class RunActiveSensor(GeodropsRachioEntity, BinarySensorEntity):
    """Read-only: on while a collapsed run is in flight. Mirrors the engine's
    persisted run marker, which only the engine sets."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, entry: GeodropsRachioConfigEntry, scheduler: Scheduler) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "run_active")
        self._scheduler = scheduler

    async def async_added_to_hass(self) -> None:
        @callback
        def _update() -> None:
            self._attr_is_on = bool(self._scheduler.store.read(RUN_ACTIVE))
            self.async_write_ha_state()
        self.async_on_remove(self._scheduler.add_listener(_update))
        _update()


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([RunActiveSensor(entry, entry.runtime_data.scheduler)])
