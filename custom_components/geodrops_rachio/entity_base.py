from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="GeoDrops + Rachio Irrigation",
        manufacturer="GeoDrops + Rachio Irrigation",
    )
