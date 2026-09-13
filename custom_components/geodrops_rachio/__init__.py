import pathlib

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, PLATFORMS
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
    return True


async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return ok
