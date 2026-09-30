from __future__ import annotations
from homeassistant.components.button import ButtonEntity, ENTITY_ID_FORMAT
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .coordinator import GeodropsRachioConfigEntry
from .engine.scheduler import Scheduler
from .entity import GeodropsRachioEntity

# Presses never queue behind one another: Stop must act mid-run.
PARALLEL_UPDATES = 0

# Buttons that drive the native scheduler: key -> entity category. Reset and
# Refresh are upkeep, not everyday controls. Names and icons are translated.
_ACTIONS: dict[str, EntityCategory | None] = {
    "run_now": None, "preview": None,
    "reset": EntityCategory.CONFIG, "refresh_runtimes": EntityCategory.CONFIG,
}
# Button key -> Scheduler coroutine method. Long actions run as background tasks
# so a press never blocks on a preview or a Rachio fetch.
_METHODS = {
    "run_now": "async_run_now", "preview": "async_preview",
    "reset": "async_reset", "refresh_runtimes": "async_refresh_runtimes",
}
_BACKGROUND = {"preview", "refresh_runtimes"}


class StopButton(GeodropsRachioEntity, ButtonEntity):
    def __init__(self, entry: GeodropsRachioConfigEntry, scheduler: Scheduler) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "stop")
        self._scheduler = scheduler

    async def async_press(self) -> None:
        await self._scheduler.request_stop()


class ActionButton(GeodropsRachioEntity, ButtonEntity):
    def __init__(self, entry: GeodropsRachioConfigEntry, scheduler: Scheduler,
                 key: str, category: EntityCategory | None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key)
        self._attr_entity_category = category
        self._entry = entry
        self._scheduler = scheduler
        self._key = key

    async def async_press(self) -> None:
        coro = getattr(self._scheduler, _METHODS[self._key])()
        if self._key in _BACKGROUND:
            self._entry.async_create_background_task(
                self.hass, coro, f"geodrops_rachio_{self._key}")
        else:
            await coro


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    scheduler = entry.runtime_data.scheduler
    entities: list[ButtonEntity] = [StopButton(entry, scheduler)]
    entities += [ActionButton(entry, scheduler, key, category)
                 for key, category in _ACTIONS.items()]
    async_add_entities(entities)
