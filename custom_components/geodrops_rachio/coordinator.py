from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .brain.calibration import consistent_count
from .brain.config import Tunables
from .engine import port
from .engine.store import EFFICACY, ZONE_WATERED
from .util import slug

if TYPE_CHECKING:
    from .engine.scheduler import Scheduler

_LOGGER = logging.getLogger(__name__)

# Scheduler defaults for the progress hint: how many agreeing samples a zone
# needs to converge, and the band they must agree within. A rare advanced
# override would shift them; the label is a hint, the state is the engine's.
_DEFAULTS = Tunables()
_CONVERGENCE_TARGET = _DEFAULTS.convergence_samples
# Why the last probe didn't count, translated for the status label.
_REJECT_PHRASES = {
    "no_rise": "probe too small",
    "small_rise": "probe too small",
    "saturated": "soil too wet",
    "rain": "rained out",
}


def format_calibration_status(state: str | None, samples: int | None,
                              consistent: int | None,
                              last_reject_reason: str | None) -> str | None:
    """One human label folding the raw calibration state together with progress
    and, when a zone is stuck, why.

      converged                         -> "Calibrated"
      calibrating, last probe rejected  -> "Calibrating — soil too wet"
      calibrating, newest 2 agree       -> "Calibrating (2 of 3 consistent)"
      calibrating, newest agrees with none of several -> "Calibrating — results vary"
      calibrating, no probes yet        -> "Calibrating"
      unresponsive                      -> "Not responding — check heads and probe"

    Progress counts the runs that agree, not the runs taken, so "3 of 3" is
    only ever calibrated. A reject reason wins over the count: it explains why
    the count is not growing, which is what someone checking a stuck zone
    actually needs."""
    if not state:
        return state
    if state == "converged":
        return "Calibrated"
    if state == "unresponsive":
        # The repair says what to check; the label points there.
        return "Not responding — check heads and probe"
    label = state.replace("_", " ").title()
    # A full count is only ever calibrated. A converged zone demoted after
    # misses keeps the samples that agreed on its new rate until the next
    # probe, and a scheduler override can draw the band differently.
    consistent = min(consistent or 0, _CONVERGENCE_TARGET - 1)
    phrase = _REJECT_PHRASES.get(last_reject_reason or "")
    if phrase:
        return f"{label} — {phrase}"
    if (samples or 0) > 1 and consistent == 1:
        return f"{label} — results vary"
    if consistent:
        return f"{label} ({consistent} of {_CONVERGENCE_TARGET} consistent)"
    return label


def parse_last_nightly(attrs: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    attrs = attrs or {}
    delivered = attrs.get("delivered_minutes") or {}
    watered = attrs.get("watered") or []
    # The record's `end` is a time-only string ("06:44") a TIMESTAMP sensor
    # can't parse. `end_iso` is the tz-aware valve-close instant (scheduler
    # >= v0.8.2); fall back to the plan-publish `updated` stamp only for records
    # from an older delivered brain or a no-water night (end_iso == "").
    # `zone_end_iso` (v1.2+) is the zone's own last valve close, which beats
    # the run's end for any zone that finished before the last one.
    own_end = (attrs.get("zone_end_iso") or {}).get(key)
    last_watered = (own_end or attrs.get("end_iso") or attrs.get("updated")
                    if key in watered else None)
    return {
        "last_delivered_runtime": delivered.get(key),
        "last_watered": last_watered,
    }


def parse_nightly_calibration(attrs: Mapping[str, Any] | None,
                              key: str) -> dict[str, Any]:
    """Per-zone calibration published live on the last_nightly record. Only the
    zones watered that night appear; missing keys fall back to the efficacy
    file (which lags and carries no `state` for excluded zones)."""
    cal = ((attrs or {}).get("calibration") or {}).get(key) or {}
    # A snapshot from before `samples` was recorded shows no count:
    # its lifetime n_obs would overstate progress after a reset.
    samples = cal.get("samples", 0)
    return {"efficacy": cal.get("efficacy"), "calibration_state": cal.get("state"),
            "samples": samples,
            # The snapshot carries no rates to compare; it only stands in for
            # a zone the efficacy store has no state for.
            "consistent": samples,
            "last_reject_reason": cal.get("last_reject_reason")}


def parse_preview(attrs: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    planned = (attrs or {}).get("planned_minutes") or {}
    return {"planned_runtime": planned.get(key)}


def parse_refill_depth(runtimes_attrs: Mapping[str, Any] | None, rachio_zone_id: str,
                       static_mm: float | None) -> dict[str, Any]:
    """Per-zone refill depth in mm (Rachio's "depth of water" for the zone).

    Prefer the live value Rachio last reported — published on the runtimes
    entity by a Refresh Runtimes pull, keyed by rachio_zone_id — and fall back
    to the value captured at wizard time. A manual zone (no rachio_zone_id), or
    a zone the live pull didn't return, keeps the static config value, so the
    sensor is never blank."""
    live = (runtimes_attrs or {}).get("refill_depths_mm") or {}
    val = live.get(rachio_zone_id) if rachio_zone_id else None
    return {"refill_depth": val if val is not None else static_mm}


def parse_efficacy(store: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    rec = (store or {}).get(key) or {}
    # Progress toward convergence is the accepted samples still in the
    # window, not lifetime n_obs (which survives a recalibration reset).
    recent = rec.get("recent") or []
    return {"efficacy": rec.get("efficacy"), "calibration_state": rec.get("state"),
            "samples": len(recent),
            # Judged on the newest sample's dose, as the engine judges it. A
            # rejected probe overwrites prior_minutes without adding a sample,
            # but its reject phrase wins over the count until the next
            # accepted probe resets both (format_calibration_status).
            "consistent": consistent_count(recent, _DEFAULTS, rec.get("prior_minutes")),
            "last_reject_reason": rec.get("last_reject_reason")}


@dataclass
class GeodropsRachioData:
    """What a running entry keeps in its runtime_data."""

    data: dict[str, Any]
    coordinator: ZoneStateCoordinator
    scheduler: Scheduler
    hub_device_id: str
    # {original id: current id} of bound entities renamed while this entry
    # runs; the engine reads through it until the reload that rebinds them.
    renamed: dict[str, str]
    # What this running entry was set up with; a reload that would not change
    # it is skipped (see async_reload_if_changed).
    setup_snapshot: dict[str, Any]
    # {zone key: target_range} the target floors were last published for; the
    # snapshot leaves targets out (see __init__._apply_targets).
    targets: dict[str, Any] = field(default_factory=dict)
    # An options dialog is open: its edits must not restart the scheduler.
    suppress_reload: bool = False
    # A bound entity was renamed; the update listener that follows rebinds.
    rename_pending: bool = False
    # A rebind is waiting for the in-flight run to end.
    reload_after_run: bool = False


type GeodropsRachioConfigEntry = ConfigEntry[GeodropsRachioData]


class ZoneStateCoordinator:
    """Fans the scheduler's records + efficacy document out to per-zone sensors."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 scheduler: Scheduler, calibrating: bool) -> None:
        self.hass = hass
        self.entry = entry
        self._scheduler = scheduler
        # Active Watering Calibration; changing it reloads the entry.
        self._calibrating = calibrating
        self._listeners: list[Callable[[], None]] = []
        self._unsub: Callable[[], None] | None = None

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
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
                _LOGGER.exception("sensor update %r failed", cb)

    def _attrs(self, name: str) -> dict[str, Any] | None:
        rec = self._scheduler.records.get(name)
        return rec["attributes"] if rec is not None else None

    def data_for(self, key: str) -> dict[str, Any]:
        out: dict[str, Any] = {"planned_runtime": None, "last_delivered_runtime": None,
                               "last_watered": None, "efficacy": None,
                               "calibration_state": None,
                               "refill_depth": None, "target_floor": None}
        ln = self._attrs("last_nightly")
        if ln is not None:
            out.update(parse_last_nightly(ln, key))
        # The zone's latest watering from any source (our runs, Rachio-native ones)
        # wins; a zone with no entry yet keeps last night's values.
        zw = (self._scheduler.store.read(ZONE_WATERED) or {}).get(key)
        if zw:
            out["last_watered"] = zw.get("end_iso")
            out["last_delivered_runtime"] = zw.get("minutes")
        # Effective target floor (need-water line) for the live Deficit sensor.
        tg = self._attrs("targets")
        if tg is not None:
            out["target_floor"] = (tg.get("target_floors") or {}).get(key)
        # Refill depth: config snapshot from the wizard, overlaid with the live
        # Rachio value when a refresh has published it.
        zcfg: dict[str, Any] = next((z for z in self.entry.data.get("zones", [])
                                     if z.get("key") == key), {})
        rt = self._attrs("runtimes")
        out.update(parse_refill_depth(
            rt if rt is not None else {},
            zcfg.get("rachio_zone_id", ""), zcfg.get("refill_depth_mm")))
        pv = self._attrs("preview")
        if pv is not None:
            out.update(parse_preview(pv, key))
        out.update(parse_efficacy(self._scheduler.store.read(EFFICACY) or {}, key))
        # The store is written at every probe finalize, so it is the freshest
        # source: where it has a `state` it wins. The nightly snapshot is frozen at
        # run time (a probe finalizing hours later never reaches it) and only
        # fills in zones the store has no state for (only excluded_since).
        if ln is not None:
            cal = parse_nightly_calibration(ln, key)
            if out["calibration_state"] is None and cal["calibration_state"] is not None:
                out["calibration_state"] = cal["calibration_state"]
                # Take progress + reject reason from the same source as the state.
                out["samples"] = cal["samples"]
                out["consistent"] = cal["consistent"]
                out["last_reject_reason"] = cal["last_reject_reason"]
            if out["efficacy"] is None and cal["efficacy"] is not None:
                out["efficacy"] = cal["efficacy"]
        # Fold the raw state + progress + why-stuck into one display label; drop
        # the helper keys so the returned dict keeps its documented shape.
        out["calibration_state"] = format_calibration_status(
            out.get("calibration_state"), out.pop("samples", 0),
            out.pop("consistent", 0), out.pop("last_reject_reason", None))
        # Nothing updates the store while calibration is off, the zone is
        # excluded or its probe is out of service, so its state would read as
        # progress frozen in place (or unknown, for a zone never calibrated).
        if not self._calibrating:
            out["calibration_state"] = "Disabled"
        elif self._scheduler.port.state(self.exclude_entity(key)) == "on":
            out["calibration_state"] = "Excluded"
        elif port.probe_out_of_service(self.active_entity(key),
                                       self._scheduler.port.state):
            out["calibration_state"] = "Inactive"
        return out

    def exclude_entity(self, key: str) -> str:
        """The zone's Exclude from Watering/Calibration switch, as configured."""
        zone: dict[str, Any] = next((z for z in self.entry.data.get("zones", [])
                                     if z.get("key") == key), {})
        # As build_config does: only a missing key takes the default.
        return str(zone.get("exclude_boolean",
                            f"switch.geodrops_rachio_{slug(key)}_exclude") or "")

    def active_entity(self, key: str) -> str | None:
        """The zone's GeoDrops probe Active switch, or None when it has none."""
        dominant = next((z.get("dominant_sensor") for z in self.entry.data.get("zones", [])
                         if z.get("key") == key), None)
        return self._scheduler.port.probe_active_entity(dominant) if dominant else None

    @callback
    def async_start(self) -> None:
        self._unsub = self._scheduler.add_listener(self._notify)

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
