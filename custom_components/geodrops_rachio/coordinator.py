from __future__ import annotations

import datetime as dt
import json
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LAST_NIGHTLY_ENTITY = "pyscript.geodrops_rachio_last_nightly"
PREVIEW_ENTITY = "pyscript.geodrops_rachio_preview"
# Published by the scheduler's refresh_runtimes service (the Refresh Runtimes
# button): a live Rachio pull, keyed by rachio_zone_id.
RUNTIMES_ENTITY = "pyscript.geodrops_rachio_runtimes"
STATE_DIRNAME = "geodrops_rachio_state"
EFFICACY_STATE_FILE = "irrigation_efficacy.json"
_FILE_REFRESH = dt.timedelta(hours=1)


def parse_last_nightly(attrs: dict, key: str) -> dict:
    attrs = attrs or {}
    delivered = attrs.get("delivered_minutes") or {}
    watered = attrs.get("watered") or []
    # The record's `end` is a time-only string ("06:44") a TIMESTAMP sensor
    # can't parse. `end_iso` is the tz-aware valve-close instant (scheduler
    # >= v0.8.2); fall back to the plan-publish `updated` stamp only for records
    # from an older delivered brain or a no-water night (end_iso == "").
    last_watered = (attrs.get("end_iso") or attrs.get("updated")
                    if key in watered else None)
    return {
        "last_delivered_runtime": delivered.get(key),
        "last_watered": last_watered,
    }


def parse_nightly_calibration(attrs: dict, key: str) -> dict:
    """Per-zone calibration published live on the last_nightly record. Only the
    zones watered that night appear; missing keys fall back to the efficacy
    file (which lags and carries no `state` for excluded zones)."""
    cal = ((attrs or {}).get("calibration") or {}).get(key) or {}
    return {"efficacy": cal.get("efficacy"), "calibration_state": cal.get("state")}


def parse_preview(attrs: dict, key: str) -> dict:
    planned = (attrs or {}).get("planned_minutes") or {}
    return {"planned_runtime": planned.get(key)}


def parse_refill_depth(runtimes_attrs: dict, rachio_zone_id: str, static_mm) -> dict:
    """Per-zone refill depth in mm (Rachio's "depth of water" for the zone).

    Prefer the live value Rachio last reported — published on the runtimes
    entity by a Refresh Runtimes pull, keyed by rachio_zone_id — and fall back
    to the value captured at wizard time. A manual zone (no rachio_zone_id), or
    a zone the live pull didn't return, keeps the static config value, so the
    sensor is never blank."""
    live = (runtimes_attrs or {}).get("refill_depths_mm") or {}
    val = live.get(rachio_zone_id) if rachio_zone_id else None
    return {"refill_depth": val if val is not None else static_mm}


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

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    def data_for(self, key: str) -> dict:
        out = {"planned_runtime": None, "last_delivered_runtime": None,
               "last_watered": None, "efficacy": None, "calibration_state": None,
               "refill_depth": None}
        ln = self.hass.states.get(LAST_NIGHTLY_ENTITY)
        if ln is not None:
            out.update(parse_last_nightly(ln.attributes, key))
        # Refill depth: config snapshot from the wizard, overlaid with the live
        # Rachio value when a refresh has published it.
        zcfg = next((z for z in self.entry.data.get("zones", [])
                     if z.get("key") == key), {})
        rt = self.hass.states.get(RUNTIMES_ENTITY)
        out.update(parse_refill_depth(
            rt.attributes if rt is not None else {},
            zcfg.get("rachio_zone_id", ""), zcfg.get("refill_depth_mm")))
        pv = self.hass.states.get(PREVIEW_ENTITY)
        if pv is not None:
            out.update(parse_preview(pv.attributes, key))
        out.update(parse_efficacy(self._efficacy, key))
        # The live nightly calibration wins over the file where it has a value:
        # the file carries no `state` for these zones (only excluded_since).
        if ln is not None:
            cal = parse_nightly_calibration(ln.attributes, key)
            if cal["calibration_state"] is not None:
                out["calibration_state"] = cal["calibration_state"]
            if cal["efficacy"] is not None:
                out["efficacy"] = cal["efficacy"]
        return out

    async def async_refresh_file(self, _now=None) -> None:
        path = self._efficacy_path()

        def _read():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError) as err:
                _LOGGER.debug("Could not read efficacy file %s: %s", path, err)
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
            self.hass, [LAST_NIGHTLY_ENTITY, PREVIEW_ENTITY, RUNTIMES_ENTITY],
            _on_entity))
        self._unsubs.append(async_track_time_interval(
            self.hass, self.async_refresh_file, _FILE_REFRESH))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
