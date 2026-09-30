from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN
from .util import slug, titleize


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Irrigation Controls",
        manufacturer="GeoDrops",
    )


# Newer Home Assistant links a device to its parent by the parent's registry id
# (`via_device_id`) and rejects the older identifier-based `via_device` when an
# entity is re-added, e.g. after the user changes its entity id.
_HAS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def zone_device_info(entry, key: str, hub_device_id: str) -> DeviceInfo:
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}:zone:{slug(key)}")},
        name=titleize(key),
        manufacturer="GeoDrops",
        model="Irrigation zone",
    )
    if _HAS_VIA_DEVICE_ID:
        info["via_device_id"] = hub_device_id
    else:
        info["via_device"] = (DOMAIN, entry.entry_id)
    return info
