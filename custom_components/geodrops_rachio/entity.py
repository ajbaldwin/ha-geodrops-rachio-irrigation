from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
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


def zone_device_info(entry: ConfigEntry, key: str, hub_device_id: str) -> DeviceInfo:
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}:zone:{slug(key)}")},
        name=titleize(key),
        manufacturer="GeoDrops",
        model="Irrigation zone",
    )
    if _HAS_VIA_DEVICE_ID:
        info["via_device_id"] = hub_device_id
    else:
        # Home Assistant before via_device_id; its DeviceInfo has this key.
        info["via_device"] = (DOMAIN, entry.entry_id)  # type: ignore[typeddict-unknown-key]
    return info


class GeodropsRachioEntity(Entity):
    """An entity on the Irrigation Controls device.

    Every entity here is pushed to (never polled) and named after its device;
    its own name and icon are translated under its object key. Its entity id
    is fixed rather than derived from the name, so the ids earlier versions
    created — and the dashboards and automations using them — stay put:
    `<platform>.geodrops_rachio_<object_key>`."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, entity_id_format: str, key: str,
                 object_key: str | None = None) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self.entity_id = entity_id_format.format(
            f"geodrops_rachio_{object_key or key}")
        self._attr_translation_key = object_key or key
        self._attr_device_info = device_info(entry)


class GeodropsRachioZoneEntity(GeodropsRachioEntity):
    """An entity on one zone's device, translated under its suffix."""

    def __init__(self, entry: ConfigEntry, entity_id_format: str, zone_key: str,
                 hub_device_id: str, suffix: str) -> None:
        s = slug(zone_key)
        super().__init__(entry, entity_id_format, f"zone_{s}_{suffix}",
                         f"{s}_{suffix}")
        self._attr_translation_key = suffix
        self._attr_device_info = zone_device_info(entry, zone_key, hub_device_id)
        self._zone_key = zone_key
