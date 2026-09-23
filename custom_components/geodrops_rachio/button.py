from __future__ import annotations
from homeassistant.components.button import ButtonEntity, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .entity_base import device_info

# Buttons that drive the native scheduler. (key, friendly name, icon).
_ACTIONS = [
    ("run_now", "Run irrigation now", "mdi:play-circle-outline"),
    ("preview", "Preview irrigation plan", "mdi:eye-outline"),
    ("reset", "Reset irrigation", "mdi:cancel"),
    ("refresh_runtimes", "Refresh Rachio runtimes", "mdi:refresh"),
]
# Button key -> Scheduler coroutine method. Long actions run as background tasks
# so a press never blocks on a preview or a Rachio fetch.
_METHODS = {
    "run_now": "async_run_now", "preview": "async_preview",
    "reset": "async_reset", "refresh_runtimes": "async_refresh_runtimes",
}
_BACKGROUND = {"preview", "refresh_runtimes"}


class StopButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, scheduler) -> None:
        self._attr_unique_id = f"{entry.entry_id}_stop"
        self._attr_name = "Stop irrigation"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_stop")
        self._attr_device_info = device_info(entry)
        self._scheduler = scheduler

    async def async_press(self) -> None:
        await self._scheduler.request_stop()


class ActionButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, scheduler, key: str, name: str, icon: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{key}")
        self._attr_device_info = device_info(entry)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    entities: list[ButtonEntity] = [StopButton(entry, scheduler)]
    entities += [ActionButton(entry, scheduler, key, name, icon)
                 for key, name, icon in _ACTIONS]
    async_add_entities(entities)
