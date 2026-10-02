"""Rachio zone I/O, the runtime cache and calibration-document access.

Ported from v0.9.15 bundled_app/geodrops_rachio.py lines 215-520 (the app's
@pyscript_compile file/HTTP helpers at 325-396 are gone: persistence is the
EngineStore and the Rachio fetch is injected as `fetch_zone_data`).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from .. import units
from ..brain import blocks, config
from .base import EngineBase, ZoneData
from .store import EFFICACY, PENDING_OBS, RUN_ACTIVE, ZONE_WATERED

_LOGGER = logging.getLogger(__name__)

RUNTIME_CACHE_TTL_S = 6 * 3600


class IOMixin(EngineBase):
    async def _fetch_zone_data(self) -> ZoneData:
        """(runtimes_minutes, refill_depths_mm, refill_spans_pts), one pass."""
        return await self._fetch_zone_data_fn()

    # ─── Rachio zone I/O (start/stop + poll-verify) ──────────────────────────

    def reset_counters(self) -> None:
        self.api_calls = 0
        self.state_polls = 0

    def poll_zone_running(self, zone_switch: str) -> bool:
        """Is this zone running? Reads the HA state machine — NOT the Rachio API.

        Counted separately from api_calls: HA's Rachio integration polls the cloud
        on its own schedule, so reading local state costs nothing against the Rachio
        budget. Keeping these out of api_calls matters now that the watch loop polls
        every CHECK_INTERVAL_S — otherwise the budget figure would balloon with
        calls that were never made.
        """
        self.state_polls += 1
        running = self._state_get(zone_switch) == "on"
        self._note_valve(zone_switch, running)
        return running

    def _note_valve(self, zone_switch: str, running: bool) -> None:
        """Remember when a polled valve closed: the first poll that sees it off
        after one saw it on. Rachio steps zones inside one schedule on its own,
        so these polls are the only per-zone record of when each finished — at
        most one poll interval late."""
        if running:
            self._valve_on.add(zone_switch)
        elif zone_switch in self._valve_on:
            self._valve_on.discard(zone_switch)
            self._valve_closed[zone_switch] = self.port.now()

    def _reset_valve_closes(self) -> None:
        self._valve_on = set()
        self._valve_closed = {}

    def _valve_close_iso(self, zone_switch: str, finished: dt.datetime) -> str:
        """When `zone_switch` last closed this run, as ISO. A valve still seen on
        at the end (an abort's stop) or never seen closing closed with the run."""
        closed = self._valve_closed.get(zone_switch)
        if zone_switch in self._valve_on or closed is None or closed > finished:
            closed = finished
        return closed.isoformat()

    def any_zone_running(self, zone_switches: Iterable[str]) -> bool:
        # pyscript has no generator expressions; use a list comprehension.
        return any([self.poll_zone_running(zs) for zs in zone_switches])

    async def start_block(self, runs: list[blocks.ZoneRun],
                          zone_switches: Mapping[str, str]) -> None:
        """Hand one back-to-back block of zone runs to Rachio as a single schedule.

        NOT `switch.turn_on`. The Rachio integration does not treat a zone switch as
        a relay: turning one on starts that zone for the integration's OWN configured
        default (`DEFAULT_MANUAL_RUN_MINS`, 10 minutes) and stops it on its own timer,
        which is what made the first live run water 10 minutes instead of 32.

        `start_multiple_zone_schedule` carries explicit per-zone durations, so the
        plan's minutes are the ones that run. It takes the whole block at once —
        zones back to back, in the order given, the same zone appearing as many
        times as the cycle-and-soak plan revisits it (the entity list is NOT
        de-duplicated, confirmed on the controller). One call per block instead of
        one start and one stop per cycle also keeps Rachio's push notifications to a
        couple per block rather than a couple per cycle.
        """
        self.api_calls += 1
        await self.port.call(
            "rachio", "start_multiple_zone_schedule", {
                "entity_id": blocks.entity_ids(runs, zone_switches),
                "duration": blocks.duration_csv(runs)})

    async def stop_zone(self, zone_switch: str) -> None:
        self.api_calls += 1
        await self.port.call("switch", "turn_off", {"entity_id": zone_switch})

    async def pause_device(self, minutes: float) -> None:
        """Pause the managed Rachio controller (halts the active schedule).

        HA rachio.pause_watering caps duration at 60 min and auto-resumes; we clamp
        and rely on an explicit resume for exact soak length, with the duration as a
        crash backstop so a crashed HA cannot leave the device paused forever.
        """
        self.api_calls += 1
        await self.port.call(
            "rachio", "pause_watering", {
                "devices": self._loaded_bindings().rachio_device_name,
                "duration": max(1, min(60, int(minutes)))})

    async def resume_device(self) -> None:
        self.api_calls += 1
        await self.port.call("rachio", "resume_watering",
                             {"devices": self._loaded_bindings().rachio_device_name})

    async def stop_device(self) -> None:
        """Stop the whole running-or-paused schedule (device level).

        Toggling a single zone switch off would let Rachio advance to the next zone
        of a collapsed schedule; this ends the schedule outright.
        """
        self.api_calls += 1
        try:
            await self.port.call("rachio", "stop_watering",
                                 {"devices": self._loaded_bindings().rachio_device_name})
        except Exception as err:
            _LOGGER.warning("stop_device (rachio.stop_watering) failed: %s", err)

    async def set_run_active(self, on: bool) -> None:
        """Persisted marker: a collapsed run is in flight (survives a restart)."""
        if on:
            await self.store.write(RUN_ACTIVE, True)
        else:
            await self.store.delete(RUN_ACTIVE)

    async def stop_all(self, zone_switches: Iterable[str]) -> None:
        for zs in zone_switches:
            try:
                await self.stop_zone(zs)
            except Exception as err:
                _LOGGER.warning("stop_all failed for %s: %s", zs, err)

    # ─── Rachio Public API: per-zone full-refill runtimes ────────────────────

    async def _refresh_zone_cache(self, force: bool = False) -> bool:
        """Refresh the cache when stale. True when usable live data is present."""
        now = self.port.now().timestamp()
        if (
            not force
            and self._runtime_cache["runtimes"]
            and now - self._runtime_cache["ts"] < RUNTIME_CACHE_TTL_S
        ):
            return True
        try:
            runtimes, depths, spans = await self._fetch_zone_data()
        except Exception as err:
            # Once per outage: each plan asks for runtimes, depths and spans.
            if not self._rachio_failing:
                self._rachio_failing = True
                _LOGGER.warning(
                    "Rachio fetch failed (%s); using static values until it "
                    "succeeds", err)
            else:
                _LOGGER.debug("Rachio fetch failed again (%s)", err)
            return False
        if self._rachio_failing:
            self._rachio_failing = False
            _LOGGER.info("Rachio fetch succeeded again")
        if runtimes:
            self._runtime_cache["ts"] = now
            self._runtime_cache["runtimes"] = runtimes
            self._runtime_cache["depths"] = depths
            self._runtime_cache["spans"] = spans
            return True
        return False

    async def get_runtimes(self, force: bool = False) -> dict[str, float]:
        """{rachio_zone_id: runtime_minutes}; {} on failure (caller falls back)."""
        if await self._refresh_zone_cache(force):
            runtimes: dict[str, float] = self._runtime_cache["runtimes"]
            return runtimes
        return {}

    async def get_refill_depths(self, force: bool = False) -> dict[str, float]:
        """{rachio_zone_id: refill_depth_mm}; {} on failure (caller falls back)."""
        if await self._refresh_zone_cache(force):
            depths: dict[str, float] = self._runtime_cache["depths"]
            return depths
        return {}

    async def get_refill_spans(self, force: bool = False) -> dict[str, float]:
        """{rachio_zone_id: refill_span_pts}; {} on failure (caller falls back)."""
        if await self._refresh_zone_cache(force):
            spans: dict[str, float] = self._runtime_cache["spans"]
            return spans
        return {}

    def _read_efficacy_store(self) -> dict[str, Any]:
        """{zone_key: {"efficacy","span_pts","state",...}} or {} when the file is absent."""
        data = self.store.read(EFFICACY)
        return data if isinstance(data, dict) else {}

    async def _write_efficacy_store(self, store: dict[str, Any]) -> None:
        await self.store.write(EFFICACY, store)

    def _rained_since_run(self, bindings: config.HABindings,
                          tun: config.Tunables) -> bool:
        """Rain-confounder flag for the settle pass: did meaningful rain fall over the
        overnight run->settle window? Uses the daily rain-accumulation gauge (resets at
        midnight), read at the mid-morning settle pass — the runs are all post-midnight,
        so today's accumulation covers the run and its settling. Missing/non-numeric
        reads fail safe to False (don't discard the observation on a gauge glitch);
        a missing gauge entity must not abort the settle pass either.
        The gauge's reading is converted from its unit into mm."""
        gauge = bindings.weather.rain_today
        try:
            val = float(self._state_get(gauge))
        except (NameError, TypeError, ValueError):
            return False
        val = units.to_scheduler(
            val, units.unit_of(self.port.attrs(gauge)), units.PRECIPITATION)
        return val > tun.rain_confounder_mm

    async def _append_pending_obs(self, records: list[dict[str, Any]]) -> None:
        """Append calibration observation stubs to the pending queue (restart-safe)."""
        async with self._obs_lock:
            existing = self.store.read(PENDING_OBS)
            if not isinstance(existing, list):
                existing = []
            existing.extend(records)
            await self.store.write(PENDING_OBS, existing)

    async def _drop_pending_obs(self, zones: Iterable[str]) -> list[str]:
        """Drop the pending calibration samples of `zones`, watered again inside
        their settle window. Returns the zones that had one."""
        async with self._obs_lock:
            existing = self.store.read(PENDING_OBS)
            if not isinstance(existing, list):
                return []
            keep = [rec for rec in existing if rec.get("zone") not in zones]
            if len(keep) == len(existing):
                return []
            await self.store.write(PENDING_OBS, keep)
        dropped: list[str] = []
        for rec in existing:
            zone = rec.get("zone")
            if zone in zones and zone not in dropped:
                dropped.append(zone)
        return dropped

    async def _record_zone_watering(
            self, zones: Iterable[str], minutes: Mapping[str, float], end_iso: str,
            trigger: str, zone_end_iso: Mapping[str, str] | None = None) -> None:
        """Record each zone's latest watering (see ZONE_WATERED): its own valve
        close from `zone_end_iso` when known, else the run's `end_iso`. A
        persistence failure must never take down the run that watered, so it
        only warns."""
        try:
            doc = self.store.read(ZONE_WATERED)
            if not isinstance(doc, dict):
                doc = {}
            for k in zones:
                doc[k] = {"end_iso": (zone_end_iso or {}).get(k) or end_iso,
                          "minutes": minutes.get(k),
                          "trigger": trigger}
            await self.store.write(ZONE_WATERED, doc)
        except Exception as err:
            _LOGGER.warning("could not record zone watering (%s)", err)
