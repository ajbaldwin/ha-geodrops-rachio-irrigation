from __future__ import annotations
import logging
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

# HACS creates one update entity per tracked repo, named `update.<slug>_update`
# where <slug> is the slugified HACS display name (hacs.json "name"):
# "GeoDrops + Rachio Irrigation" -> "geodrops_rachio_irrigation". If a live HACS
# install shows a different id (Developer Tools -> States, filter `update.`),
# change it here.
HACS_UPDATE_ENTITY = "update.geodrops_rachio_irrigation_update"


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

    # Observability: record whether the watched entity exists yet, so a wrong
    # id surfaces in the log instead of failing silently. The tracker still
    # binds by id and will fire once HACS creates/updates the entity.
    if hass.states.get(HACS_UPDATE_ENTITY) is None:
        _LOGGER.debug(
            "HACS update entity %s not present yet; brain-update watcher will "
            "bind when it appears — confirm the id if updates never auto-apply",
            HACS_UPDATE_ENTITY)
    else:
        _LOGGER.debug("Watching %s for scheduler-brain updates", HACS_UPDATE_ENTITY)

    return async_track_state_change_event(hass, [HACS_UPDATE_ENTITY], _on_update)
