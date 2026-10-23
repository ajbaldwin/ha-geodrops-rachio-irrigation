"""The engine's only window onto Home Assistant.

Every read of HA state, service call, sleep and clock read the engine makes goes
through an HAPort, so the same engine code runs against a live Home Assistant
(HassPort) and against the simulated world the tests use (tests/engine/world.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

# The ha-geodrops-hacs integration's domain, and the unique_id suffixes of the
# per-probe "Last Reading" timestamp sensor (`<serial>_last_reading`) and the
# "Active" switch (`<serial>_active`) it creates. Older GeoDrops versions have
# no Active switch.
GEODROPS_DOMAIN = "geodrops"
LAST_READING_SUFFIX = "_last_reading"
ACTIVE_SUFFIX = "_active"


def geodrops_sibling(hass: HomeAssistant, entity_id: str, domain: str,
                     suffix: str) -> str | None:
    """The `domain` entity whose unique_id ends in `suffix` on the same GeoDrops
    probe as `entity_id`, or None when `entity_id` is not a GeoDrops entity or
    its probe has no such entity."""
    reg = er.async_get(hass)
    entry = reg.async_get(entity_id)
    if entry is None or entry.platform != GEODROPS_DOMAIN or not entry.device_id:
        return None
    for sibling in er.async_entries_for_device(
            reg, entry.device_id, include_disabled_entities=True):
        if (sibling.platform == GEODROPS_DOMAIN and sibling.domain == domain
                and sibling.unique_id.endswith(suffix)):
            return sibling.entity_id
    return None


def probe_inactive(state: str | None) -> bool:
    """True only when a probe's Active switch reads an explicit "off".

    Anything else keeps the probe in service: no switch (a GeoDrops version
    without it), a disabled switch (no state), or one GeoDrops is not
    providing (unavailable/unknown, e.g. not loaded or downgraded)."""
    return state == "off"


class HAPort(Protocol):
    def state(self, entity_id: str) -> str | None: ...

    def attrs(self, entity_id: str) -> dict[str, Any]: ...

    def last_updated(self, entity_id: str) -> dt.datetime | None: ...

    def reading_time_entity(self, entity_id: str) -> str | None: ...

    def probe_active_entity(self, entity_id: str) -> str | None: ...

    async def call(self, domain: str, service: str, data: dict[str, Any], *,
                   blocking: bool = False) -> None: ...

    async def sleep(self, seconds: float) -> None: ...

    def now(self) -> dt.datetime: ...


class HassPort:
    """HAPort over a live HomeAssistant instance.

    `resolve` maps an entity_id the config names to the id to use now; the
    integration passes owned_entities.resolver so its own entities are still
    found after a user renames them. Applied to every read and to a call's
    `entity_id`."""

    def __init__(self, hass: HomeAssistant,
                 resolve: Callable[[str], str] | None = None) -> None:
        self.hass = hass
        self._resolve = resolve or (lambda entity_id: entity_id)

    def state(self, entity_id: str) -> str | None:
        st = self.hass.states.get(self._resolve(entity_id))
        return st.state if st is not None else None

    def attrs(self, entity_id: str) -> dict[str, Any]:
        st = self.hass.states.get(self._resolve(entity_id))
        return dict(st.attributes) if st is not None else {}

    def last_updated(self, entity_id: str) -> dt.datetime | None:
        st = self.hass.states.get(self._resolve(entity_id))
        return st.last_updated if st is not None else None

    def reading_time_entity(self, entity_id: str) -> str | None:
        """The GeoDrops "Last Reading" sensor on the same probe as `entity_id`,
        or None when `entity_id` is not a GeoDrops entity (or has no such
        sibling): another integration's sensor, a template, or a missing one."""
        return geodrops_sibling(self.hass, self._resolve(entity_id), "sensor",
                                LAST_READING_SUFFIX)

    def probe_active_entity(self, entity_id: str) -> str | None:
        """The GeoDrops "Active" switch on the same probe as `entity_id`, or None
        when it has none: not a GeoDrops entity, or a GeoDrops version without
        the switch."""
        return geodrops_sibling(self.hass, self._resolve(entity_id), "switch",
                                ACTIVE_SUFFIX)

    async def call(self, domain: str, service: str, data: dict[str, Any], *,
                   blocking: bool = False) -> None:
        target = data.get("entity_id")
        if isinstance(target, str):
            data = {**data, "entity_id": self._resolve(target)}
        elif isinstance(target, list):
            data = {**data, "entity_id": [self._resolve(e) for e in target]}
        await self.hass.services.async_call(domain, service, data, blocking=blocking)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def now(self) -> dt.datetime:
        return dt_util.now()
