"""The engine's only window onto Home Assistant.

Every read of HA state, service call, sleep and clock read the engine makes goes
through an HAPort, so the same engine code runs against a live Home Assistant
(HassPort) and against the simulated world the tests use (tests/engine/world.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


class HAPort(Protocol):
    def state(self, entity_id: str) -> str | None: ...

    def attrs(self, entity_id: str) -> dict: ...

    def last_updated(self, entity_id: str) -> dt.datetime | None: ...

    async def call(self, domain: str, service: str, data: dict, *,
                   blocking: bool = False) -> None: ...

    async def sleep(self, seconds: float) -> None: ...

    def now(self) -> dt.datetime: ...


class HassPort:
    """HAPort over a live HomeAssistant instance."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def state(self, entity_id: str) -> str | None:
        st = self.hass.states.get(entity_id)
        return st.state if st is not None else None

    def attrs(self, entity_id: str) -> dict:
        st = self.hass.states.get(entity_id)
        return dict(st.attributes) if st is not None else {}

    def last_updated(self, entity_id: str) -> dt.datetime | None:
        st = self.hass.states.get(entity_id)
        return st.last_updated if st is not None else None

    async def call(self, domain: str, service: str, data: dict, *,
                   blocking: bool = False) -> None:
        await self.hass.services.async_call(domain, service, data, blocking=blocking)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def now(self) -> dt.datetime:
        return dt_util.now()
