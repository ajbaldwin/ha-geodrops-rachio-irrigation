from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .engine.store import EFFICACY

_LOGGER = logging.getLogger(__name__)

# Scheduler default `convergence_samples` — the accepted-probe count a zone needs
# to converge. Only the denominator of a progress hint; a rare advanced override
# would change it, but the reason phrase (below) carries the important signal.
_CONVERGENCE_TARGET = 3
# Why the last probe didn't count, translated for the status label.
_REJECT_PHRASES = {
    "no_rise": "probe too small",
    "saturated": "soil too wet",
    "rain": "rained out",
}


def format_calibration_status(state, n_obs, last_reject_reason) -> str | None:
    """One human label folding the raw calibration state together with progress
    and, when a zone is stuck, why.

      converged                         -> "Converged"
      calibrating, last probe rejected  -> "Calibrating — soil too wet"
      calibrating, 2 accepted probes    -> "Calibrating (2/3)"
      calibrating, no probes yet        -> "Calibrating"

    A reject reason wins over the count: it explains why the count is not
    growing, which is what someone checking a stuck zone actually needs."""
    if not state:
        return state
    label = state.replace("_", " ").title()
    if state == "converged":
        return label
    phrase = _REJECT_PHRASES.get(last_reject_reason)
    if phrase:
        return f"{label} — {phrase}"
    if n_obs:
        return f"{label} ({n_obs}/{_CONVERGENCE_TARGET})"
    return label


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
    return {"efficacy": cal.get("efficacy"), "calibration_state": cal.get("state"),
            "n_obs": cal.get("n_obs", 0),
            "last_reject_reason": cal.get("last_reject_reason")}


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
    return {"efficacy": rec.get("efficacy"), "calibration_state": rec.get("state"),
            "n_obs": rec.get("n_obs", 0),
            "last_reject_reason": rec.get("last_reject_reason")}


class ZoneStateCoordinator:
    """Fans the scheduler's records + efficacy document out to per-zone sensors."""

    def __init__(self, hass: HomeAssistant, entry, scheduler) -> None:
        self.hass = hass
        self.entry = entry
        self._scheduler = scheduler
        self._listeners: list = []
        self._unsub = None

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        # Runs inside engine code (record publish / store write): one sensor's
        # failing state write must not skip the others or break the run.
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("geodrops_rachio: sensor update %r failed", cb)

    def _attrs(self, name: str) -> dict | None:
        rec = self._scheduler.records.get(name)
        return rec["attributes"] if rec is not None else None

    def data_for(self, key: str) -> dict:
        out = {"planned_runtime": None, "last_delivered_runtime": None,
               "last_watered": None, "efficacy": None, "calibration_state": None,
               "refill_depth": None, "target_floor": None}
        ln = self._attrs("last_nightly")
        if ln is not None:
            out.update(parse_last_nightly(ln, key))
        # Effective target floor (need-water line) for the live Deficit sensor.
        tg = self._attrs("targets")
        if tg is not None:
            out["target_floor"] = (tg.get("target_floors") or {}).get(key)
        # Refill depth: config snapshot from the wizard, overlaid with the live
        # Rachio value when a refresh has published it.
        zcfg = next((z for z in self.entry.data.get("zones", [])
                     if z.get("key") == key), {})
        rt = self._attrs("runtimes")
        out.update(parse_refill_depth(
            rt if rt is not None else {},
            zcfg.get("rachio_zone_id", ""), zcfg.get("refill_depth_mm")))
        pv = self._attrs("preview")
        if pv is not None:
            out.update(parse_preview(pv, key))
        out.update(parse_efficacy(self._scheduler.store.read(EFFICACY) or {}, key))
        # The live nightly calibration wins over the file where it has a value:
        # the file carries no `state` for these zones (only excluded_since).
        if ln is not None:
            cal = parse_nightly_calibration(ln, key)
            if cal["calibration_state"] is not None:
                out["calibration_state"] = cal["calibration_state"]
                # Take progress + reject reason from the same source as the state.
                out["n_obs"] = cal["n_obs"]
                out["last_reject_reason"] = cal["last_reject_reason"]
            if cal["efficacy"] is not None:
                out["efficacy"] = cal["efficacy"]
        # Fold the raw state + progress + why-stuck into one display label; drop
        # the helper keys so the returned dict keeps its documented shape.
        out["calibration_state"] = format_calibration_status(
            out.get("calibration_state"), out.pop("n_obs", 0),
            out.pop("last_reject_reason", None))
        return out

    @callback
    def async_start(self) -> None:
        self._unsub = self._scheduler.add_listener(self._notify)

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
