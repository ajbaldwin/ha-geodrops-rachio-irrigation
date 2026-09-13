from __future__ import annotations
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

HACS_UPDATE_ENTITY = "update.geodrops_rachio_update"


def decide_action(*, wrapper_changed: bool, brain_changed: bool) -> str:
    if wrapper_changed:
        return "restart"
    if brain_changed:
        return "reload"
    return "noop"


@callback
def async_register_update_listener(hass: HomeAssistant, entry):
    async def _on_update(event):
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if old is None or new is None:
            return
        if old.attributes.get("installed_version") == new.attributes.get("installed_version"):
            return
        await hass.config_entries.async_reload(entry.entry_id)

    return async_track_state_change_event(hass, [HACS_UPDATE_ENTITY], _on_update)
