from __future__ import annotations
from homeassistant.components.button import ButtonEntity, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .entity_base import device_info

# Buttons that forward to the scheduler's pyscript @service actions
# (pyscript.geodrops_rachio_<key>). (key, friendly name, icon).
_ACTIONS = [
    ("run_now", "Run irrigation now", "mdi:play-circle-outline"),
    ("preview", "Preview irrigation plan", "mdi:eye-outline"),
    ("reset", "Reset irrigation", "mdi:cancel"),
    ("refresh_runtimes", "Refresh Rachio runtimes", "mdi:refresh"),
]


class StopButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_stop"
        self._attr_name = "Stop irrigation"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_stop")
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        # The base class sets the entity's state to the press timestamp, which
        # the pyscript @state_trigger("button.geodrops_rachio_stop") observes.
        # The actual stop logic lives in the pyscript script, not here.
        return


class ActionButton(ButtonEntity):
    """Forwards a press to the scheduler's pyscript action of the same name."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str, name: str, icon: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{key}")
        self._attr_device_info = device_info(entry)
        self._service = f"geodrops_rachio_{key}"

    async def async_press(self) -> None:
        # Fire-and-forget: the pyscript action runs in its own task; a run/preview
        # can be long, and the button press should not block on it.
        await self.hass.services.async_call(
            "pyscript", self._service, blocking=False)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    entities: list[ButtonEntity] = [StopButton(entry)]
    entities += [ActionButton(entry, key, name, icon)
                 for key, name, icon in _ACTIONS]
    async_add_entities(entities)
