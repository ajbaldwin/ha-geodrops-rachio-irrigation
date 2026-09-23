"""Rachio zone I/O, the runtime cache and calibration-document access.

Ported from tests/legacy/geodrops_rachio_legacy.py lines 215-520 (the app's
@pyscript_compile file/HTTP helpers at 325-396 are gone: persistence is the
EngineStore and the Rachio fetch is injected as `fetch_zone_data`).
"""
from __future__ import annotations

import logging

from ..brain import blocks
from .store import EFFICACY, PENDING_OBS

_LOGGER = logging.getLogger(__name__)

RUNTIME_CACHE_TTL_S = 6 * 3600


class IOMixin:
    async def _fetch_zone_data(self):
        """(runtimes_minutes, refill_depths_mm, refill_spans_pts), one pass."""
        # _current_bindings may be unset only if this is called before any config
        # load at all; fall back to the documented default rather than crash.
        key_name = (
            self._current_bindings.rachio_api_key_secret if self._current_bindings
            else "rachio_api_key"
        )
        return await self._fetch_zone_data_fn(key_name)

    # ─── Rachio zone I/O (start/stop + poll-verify) ──────────────────────────

    def reset_counters(self):
        self.api_calls = 0
        self.state_polls = 0

    def poll_zone_running(self, zone_switch):
        """Is this zone running? Reads the HA state machine — NOT the Rachio API.

        Counted separately from api_calls: HA's Rachio integration polls the cloud
        on its own schedule, so reading local state costs nothing against the Rachio
        budget. Keeping these out of api_calls matters now that the watch loop polls
        every CHECK_INTERVAL_S — otherwise the budget figure would balloon with
        calls that were never made.
        """
        self.state_polls += 1
        return self._state_get(zone_switch) == "on"

    def any_zone_running(self, zone_switches):
        # pyscript has no generator expressions; use a list comprehension.
        return any([self.poll_zone_running(zs) for zs in zone_switches])

    async def start_block(self, runs, zone_switches):
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

    async def stop_zone(self, zone_switch):
        self.api_calls += 1
        await self.port.call("switch", "turn_off", {"entity_id": zone_switch})

    async def pause_device(self, minutes):
        """Pause the managed Rachio controller (halts the active schedule).

        HA rachio.pause_watering caps duration at 60 min and auto-resumes; we clamp
        and rely on an explicit resume for exact soak length, with the duration as a
        crash backstop so a crashed HA cannot leave the device paused forever.
        """
        self.api_calls += 1
        await self.port.call(
            "rachio", "pause_watering", {
                "devices": self._current_bindings.rachio_device_name,
                "duration": max(1, min(60, int(minutes)))})

    async def resume_device(self):
        self.api_calls += 1
        await self.port.call("rachio", "resume_watering",
                             {"devices": self._current_bindings.rachio_device_name})

    async def stop_device(self):
        """Stop the whole running-or-paused schedule (device level).

        Toggling a single zone switch off would let Rachio advance to the next zone
        of a collapsed schedule; this ends the schedule outright.
        """
        self.api_calls += 1
        try:
            await self.port.call("rachio", "stop_watering",
                                 {"devices": self._current_bindings.rachio_device_name})
        except Exception as err:
            _LOGGER.warning(f"irrigation: stop_device (rachio.stop_watering) failed: {err}")

    async def set_run_active(self, on):
        """Persisted marker: a collapsed run is in flight (survives a restart)."""
        await self.port.call("switch", "turn_on" if on else "turn_off",
                             {"entity_id": self._current_bindings.run_active_boolean})

    async def stop_all(self, zone_switches):
        for zs in zone_switches:
            try:
                await self.stop_zone(zs)
            except Exception as err:
                _LOGGER.warning(f"irrigation: stop_all failed for {zs}: {err}")

    # ─── Rachio Public API: per-zone full-refill runtimes ────────────────────

    async def _refresh_zone_cache(self, force=False):
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
            _LOGGER.warning(
                f"irrigation: Rachio fetch failed ({err}); using static values"
            )
            return False
        if runtimes:
            self._runtime_cache["ts"] = now
            self._runtime_cache["runtimes"] = runtimes
            self._runtime_cache["depths"] = depths
            self._runtime_cache["spans"] = spans
            return True
        return False

    async def get_runtimes(self, force=False):
        """{rachio_zone_id: runtime_minutes}; {} on failure (caller falls back)."""
        if await self._refresh_zone_cache(force):
            return self._runtime_cache["runtimes"]
        return {}

    async def get_refill_depths(self, force=False):
        """{rachio_zone_id: refill_depth_mm}; {} on failure (caller falls back)."""
        if await self._refresh_zone_cache(force):
            return self._runtime_cache["depths"]
        return {}

    async def get_refill_spans(self, force=False):
        """{rachio_zone_id: refill_span_pts}; {} on failure (caller falls back)."""
        if await self._refresh_zone_cache(force):
            return self._runtime_cache["spans"]
        return {}

    def _read_efficacy_store(self):
        """{zone_key: {"efficacy","span_pts","state",...}} or {} when the file is absent."""
        data = self.store.read(EFFICACY)
        return data if isinstance(data, dict) else {}

    async def _write_efficacy_store(self, store):
        await self.store.write(EFFICACY, store)

    def _rained_since_run(self, bindings, tun):
        """Rain-confounder flag for the settle pass: did meaningful rain fall over the
        overnight run->settle window? Uses the daily rain-accumulation gauge (resets at
        midnight), read at the mid-morning settle pass — the runs are all post-midnight,
        so today's accumulation covers the run and its settling. Missing/non-numeric
        reads fail safe to False (don't discard the observation on a gauge glitch)."""
        try:
            val = float(self._state_get(bindings.weather.rain_today))
        except (TypeError, ValueError):
            return False
        return val > tun.rain_confounder_mm

    async def _append_pending_obs(self, records):
        """Append calibration observation stubs to the pending queue (restart-safe)."""
        existing = self.store.read(PENDING_OBS)
        if not isinstance(existing, list):
            existing = []
        existing.extend(records)
        await self.store.write(PENDING_OBS, existing)
