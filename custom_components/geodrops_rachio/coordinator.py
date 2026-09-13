from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LAST_NIGHTLY_ENTITY = "pyscript.geodrops_rachio_last_nightly"
PREVIEW_ENTITY = "pyscript.geodrops_rachio_preview"
STATE_DIRNAME = "geodrops_rachio_state"
EFFICACY_STATE_FILE = "irrigation_efficacy.json"
_FILE_REFRESH = dt.timedelta(hours=1)


def parse_last_nightly(attrs: dict, key: str) -> dict:
    delivered = (attrs or {}).get("delivered_minutes") or {}
    watered = (attrs or {}).get("watered") or []
    return {
        "last_delivered_runtime": delivered.get(key),
        "last_watered": (attrs or {}).get("end") if key in watered else None,
    }


def parse_preview(attrs: dict, key: str) -> dict:
    planned = (attrs or {}).get("planned_minutes") or {}
    return {"planned_runtime": planned.get(key)}


def parse_efficacy(store: dict, key: str) -> dict:
    rec = (store or {}).get(key) or {}
    return {"efficacy": rec.get("efficacy"), "calibration_state": rec.get("state")}


class ZoneStateCoordinator:
    """Fans the scheduler's published state + efficacy file out to per-zone sensors."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._efficacy: dict = {}
        self._listeners: list = []
        self._unsubs: list = []

    def _efficacy_path(self) -> str:
        return self.hass.config.path("pyscript", STATE_DIRNAME, EFFICACY_STATE_FILE)

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    @callback
    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    def data_for(self, key: str) -> dict:
        out = {"planned_runtime": None, "last_delivered_runtime": None,
               "last_watered": None, "efficacy": None, "calibration_state": None}
        ln = self.hass.states.get(LAST_NIGHTLY_ENTITY)
        if ln is not None:
            out.update(parse_last_nightly(ln.attributes, key))
        pv = self.hass.states.get(PREVIEW_ENTITY)
        if pv is not None:
            out.update(parse_preview(pv.attributes, key))
        out.update(parse_efficacy(self._efficacy, key))
        return out

    async def async_refresh_file(self, _now=None) -> None:
        path = self._efficacy_path()

        def _read():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

        self._efficacy = await self.hass.async_add_executor_job(_read)
        self._notify()

    async def async_start(self) -> None:
        await self.async_refresh_file()

        @callback
        def _on_entity(_event):
            # last_nightly change usually means calibration ran too -> reread file.
            self.hass.async_create_task(self.async_refresh_file())

        self._unsubs.append(async_track_state_change_event(
            self.hass, [LAST_NIGHTLY_ENTITY, PREVIEW_ENTITY], _on_entity))
        self._unsubs.append(async_track_time_interval(
            self.hass, self.async_refresh_file, _FILE_REFRESH))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
