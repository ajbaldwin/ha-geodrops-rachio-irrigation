import pathlib

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PLATFORMS
from .util import slug
from . import delivery, updater


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    pyscript_dir = pathlib.Path(hass.config.path("pyscript"))
    bundled_dir = pathlib.Path(__file__).parent / "bundled_app"
    try:
        await delivery.async_deliver(
            hass, dict(entry.data), pyscript_dir=pyscript_dir, bundled_dir=bundled_dir)
    except ValueError as err:  # invalid advanced_overrides
        raise ConfigEntryNotReady(str(err)) from err

    from .coordinator import ZoneStateCoordinator
    coordinator = ZoneStateCoordinator(hass, entry)
    await coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "data": dict(entry.data), "coordinator": coordinator}
    entry.async_on_unload(coordinator.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(updater.async_register_update_listener(hass, entry))
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    _purge_orphan_zone_devices(hass, entry)
    return True


def _purge_orphan_zone_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    reg = dr.async_get(hass)
    keep = {f"{entry.entry_id}:zone:{slug(z['key'])}"
            for z in entry.data.get("zones", [])}
    for device in dr.async_entries_for_config_entry(reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain == DOMAIN and ":zone:" in ident and ident not in keep:
                reg.async_remove_device(device.id)
                break


async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # An options flow persists each edit via async_update_entry, which fires
    # this listener. While the dialog is open we defer the scheduler restart:
    # the options flow sets suppress_reload and, on "Done", clears it and fires
    # exactly one reload. See config_flow.GeodropsRachioOptionsFlow.
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if store.get("suppress_reload"):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return ok
