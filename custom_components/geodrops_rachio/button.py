from __future__ import annotations
from homeassistant.components.button import ButtonEntity, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .entity_base import device_info


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([StopButton(entry)])
