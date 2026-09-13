from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN
from .util import slug


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="GeoDrops + Rachio Irrigation",
        manufacturer="GeoDrops",
    )


def zone_device_info(entry, key: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}:zone:{slug(key)}")},
        name=str(key),
        manufacturer="GeoDrops",
        model="Irrigation zone",
        via_device=(DOMAIN, entry.entry_id),
    )
