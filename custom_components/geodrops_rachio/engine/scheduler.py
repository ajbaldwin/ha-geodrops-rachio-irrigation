"""The Scheduler: every engine mixin composed, plus triggers and lifecycle.

Ported from bundled_app/geodrops_rachio.py lines 2461-2464 and 2604-2847
(triggers, startup recovery, services), with pyscript's task.unique replaced by
one owned run task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started

from ..brain import recovery
from .base import EngineBase
from .io import IOMixin
from .learning import LearningMixin
from .orchestration import OrchestrationMixin
from .planning import PlanningMixin
from .runner import RunnerMixin

_LOGGER = logging.getLogger(__name__)

SAFETY_STOP_TIMEOUT_S = 10


async def _cancel_and_wait(task: asyncio.Task | None) -> None:
    """Cancel `task` and wait for it to unwind (its `finally` blocks run).

    The task's own CancelledError is absorbed — the caller asked for it — but a
    cancellation aimed at the CALLER while it waits is re-raised, never eaten.
    An ordinary exception raised while the task unwinds is logged, not
    propagated: the caller (reset / shutdown) still has teardown to do.
    """
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception as err:
        _LOGGER.warning(f"irrigation: cancelled task {task.get_name()} failed while "
                        f"unwinding ({err})")


class Scheduler(LearningMixin, OrchestrationMixin, PlanningMixin, RunnerMixin,
                IOMixin, EngineBase):
    def __init__(self, port, store, load_raw_config, fetch_zone_data,
                 create_task: Callable[[Coroutine, str], asyncio.Task]) -> None:
        super().__init__(port, store, load_raw_config, fetch_zone_data)
        self._create_task = create_task
        self.run_task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self._unsubs: list[Callable[[], None]] = []

    # --- the one run task (replaces task.unique("geodrops_rachio_run")) -------
    async def _cancel_run(self) -> None:
        task, self.run_task = self.run_task, None
        await _cancel_and_wait(task)

    async def _start_run(self, wait: bool, trigger: str) -> None:
        await self._cancel_run()
        self.run_task = self._create_task(
            self._plan_and_run(wait, trigger), "geodrops_rachio_run")

    # --- triggers ---------------------------------------------------------
    async def irrigation_nightly(self, _now=None) -> None:
        await self._start_run(True, "nightly")

    # --- ported: legacy lines 2604-2742 ---------------------------------------
    async def _on_startup(self):
        """Safety net after any HA restart or pyscript reload.

        A run interrupted mid-watering leaves a Rachio valve OPEN: pyscript is killed
        before it can stop the zone, the run is NOT resumed (the system is stateless),
        and no recap is sent. Without this, the only backstop is the 6-hour stuck-zone
        automation. On startup we poll the managed zones and close anything still
        running, noting it in the Logbook. A short sleep first lets the Rachio
        integration load its switch entities before we poll them.

        If we DID find an open valve, we then self-heal: re-plan and finish the run.
        Gating the self-heal on "an orphan was found" is what keeps it safe without
        persisted state — an open valve proves the run was interrupted mid-watering
        rather than completed, so we cannot double-water a zone that already
        finished. (GeoDrops moisture lags watering by a while, so a naive
        "re-evaluate on every restart" WOULD re-water a just-finished zone.) The
        trade-off: the zone that was interrupted re-waters from scratch, so it gets
        its partial cycle plus a full one. A restart during the pre-dawn wait, with
        nothing yet open, is indistinguishable from a completed run and is therefore
        skipped — that night is simply missed, costing no water.
        """
        await self.port.sleep(30)
        # pyscript re-creates its entities, so the status would read `unknown` until
        # the next run. Publish it now so a filtered Logbook and any dashboard card
        # have something to point at from the moment HA comes back.
        self._set_status("idle")
        # Bring back last night's record and the calibration series. HA does not
        # restore pyscript entities, so without this a restart erases the evidence
        # for the very night someone is about to ask about.
        self._restore_records()
        # Recompute per-zone target floors now, so a Deficit sensor has a fresh
        # target from the moment HA comes back — not only after the first nightly.
        # Read-only (no efficacy writes), so it is safe outside the run window and
        # must not gate on it. A load/sensor failure here must never take down the
        # safety check below.
        try:
            self._current_cfg = self._load_cfg()
            self._current_bindings = self._current_cfg.bindings
            await self._publish_targets(self._current_cfg)
        except Exception as err:
            _LOGGER.warning(f"irrigation: startup target-floor publish skipped ({err})")
        # Time gate: only clean up during the overnight run window (~22:00-07:00).
        # A daytime restart must not stop a syringe / pet-cleanup run that shares a
        # managed zone. (Only managed zones are ever polled — see below.)
        now = self._naive_now()
        if not (now.hour >= 22 or now.hour <= 7):
            return
        try:
            cfg = self._load_cfg()
        except Exception as err:
            _LOGGER.warning(f"irrigation: startup safety check skipped; config load failed ({err})")
            return
        self._current_cfg = cfg
        self._current_bindings = cfg.bindings

        marker_state = self.port.state(self._current_bindings.run_active_boolean)
        if marker_state is None:
            # A missing marker helper (supported when use_pause_collapse is off) must
            # degrade to "no interrupted collapsed run", never crash the safety check.
            marker_set = False
        else:
            marker_set = marker_state == "on"

        running = []
        for zone in cfg.zones.values():
            try:
                if self.poll_zone_running(zone.rachio_switch):
                    running.append(zone.rachio_switch)
            except Exception:
                pass

        if marker_set:
            # A collapsed run was in flight — mid-water OR mid-pause (all valves read
            # off during a pause, so the open-valve test below cannot see it). Kill
            # any running or about-to-auto-resume schedule outright, then self-heal.
            await self.stop_device()
            if running:
                await self.stop_all(running)
            await self.set_run_active(False)
            await self._activity(
                "Startup safety: an interrupted collapsed run was detected (marker "
                "set); stopped the Rachio schedule and re-planning from live moisture"
            )
            await self._start_run(True, "startup-heal")
            return

        if not running:
            # No collapsed-run marker and nothing open. Before giving up, check the
            # waiting marker: a nightly that was still WAITING for its pre-dawn window
            # had opened no valve, so it is invisible to every check above and would
            # otherwise be lost silently (the 2026-08-30 02:28 OOM did exactly this).
            # The marker IS the persisted state that makes re-arming safe here — a
            # waiting run delivered zero, so re-planning from live moisture cannot
            # double-water. Consume it (read then always clear) and act on the
            # verdict.
            marker = self._read_waiting_marker()
            await self._clear_waiting_marker()
            action = recovery.startup_action(marker, self._naive_now().isoformat())
            if action == recovery.RE_ARM:
                await self._activity(
                    "Startup: a nightly run was waiting for its pre-dawn window when "
                    "HA restarted; re-planning from live moisture"
                )
                await self._start_run(True, "startup-heal")
            elif action == recovery.MISSED:
                # Came back after the window closed: nothing to water. Record the
                # night as missed so the morning-after query is answerable.
                stamp = marker.get("stamp") or self._naive_now().isoformat(
                    timespec="seconds")
                trigger = marker.get("trigger") or "nightly"
                await self._publish_last_run(stamp, trigger, skipped="missed-restart")
                self._set_status("skipped", detail="missed — restart after window closed")
                await self._activity(
                    "Startup: a nightly run was waiting when HA restarted, but the "
                    "watering window had already closed; recorded as missed"
                )
            # IGNORE: no run was waiting (or the marker was malformed). Either no run
            # was in flight or one had already finished — indistinguishable without
            # state, so we do NOT self-heal (see the docstring's double-watering note).
            return

        await self.stop_all(running)
        await self._activity(
            f"Startup safety: closed {len(running)} zone(s) left open by an "
            f"interrupted run ({', '.join(running)}); no recap was sent for that run"
        )

        # Self-heal. An orphaned open valve is proof the run was interrupted
        # MID-WATERING rather than completed, which is what makes re-planning safe:
        # a completed run could not have left a valve open, so we cannot be
        # re-watering zones that already finished. Re-evaluate from live moisture and
        # finish whatever still needs water in the time left before dawn (the cap is
        # clamped to the remaining window in _plan_context).
        await self._activity("Startup: re-planning the interrupted run from live moisture")
        await self._start_run(True, "startup-heal")

    # --- button actions ---------------------------------------------------
    async def async_run_now(self) -> None:
        """Run the full plan immediately (no pre-dawn wait). Waters for real."""
        await self._start_run(False, "run_now")

    async def async_preview(self) -> None:
        """Dry run: report the plan that would execute, without watering."""
        await self._preview()

    async def request_stop(self) -> None:
        """The Stop button: raise the manual-stop flag the watch loop honours."""
        self._manual_stop = True
        await self._activity("Stop button pressed")

    # --- ported: legacy lines 2766-2791 ---------------------------------------
    async def async_reset(self):
        """Cancel a planned or in-progress run and reset the system to idle.

        irrigation_stop only raises the manual-stop flag, which the in-run watch
        honours once watering is underway; it does NOT interrupt the multi-hour
        pre-dawn "waiting" sleep, so a planned-but-unstarted run keeps its slot until
        its start time. This kills the waiting or watering task outright, closes any
        open valves, clears the waiting + run-active markers, and returns status to
        idle. Safe to call in any state.
        """
        await self._cancel_run()  # terminate the waiting or watering run task now
        self._manual_stop = False
        await self.stop_device()  # stop any running/paused Rachio schedule (self-guarding)
        try:
            await self.stop_all([z.rachio_switch for z in self._current_cfg.zones.values()])
        except Exception:
            pass  # no config loaded / nothing open at the zone level
        try:
            await self._clear_waiting_marker()
            await self.set_run_active(False)
        except Exception as err:
            _LOGGER.warning(f"irrigation: reset — marker cleanup skipped: {err}")
        self._set_status("idle", detail="reset")
        await self._activity("Manual reset — planned/running run cancelled; system idle")

    # --- ported: legacy lines 2794-2834 ---------------------------------------
    async def async_refresh_runtimes(self):
        """Force a live Rachio runtime fetch and record the result in the HA Logbook
        (activity log) plus a status state — checkable without the system log:
          1. Logbook entry named "Irrigation" (Settings -> Logbook / activity log);
          2. state  pyscript.geodrops_rachio_runtimes  (Developer Tools -> States) — value
             is the zone count; source/live/updated/runtimes_minutes are attributes.
        A live pull yields a non-empty dict keyed by rachio_zone_id; an empty dict
        means the fetch failed and the scheduler is on static config.yaml runtimes.
        """
        if self._run_in_progress:
            _LOGGER.warning(
                "irrigation: runtime refresh skipped — an irrigation run is in "
                "progress and owns the shared config globals"
            )
            return
        # Standalone service, outside _plan_and_run — load config here so
        # get_runtimes()'s Rachio fetch has _current_bindings.rachio_api_key_secret
        # to look up, same as every other entry point that can reach _fetch_zone_data.
        cfg = self._load_cfg()
        self._current_cfg = cfg
        self._current_bindings = cfg.bindings
        runtimes = await self.get_runtimes(force=True)
        depths = (await self.get_refill_depths()) if runtimes else {}
        live = bool(runtimes)
        source = "live Rachio" if live else "FAILED — using static config.yaml runtimes"
        stamp = self._naive_now().isoformat(timespec="seconds")

        self._publish(
            "runtimes",
            len(runtimes),
            {
                "source": source,
                "live": live,
                "updated": stamp,
                "runtimes_minutes": runtimes,
                "refill_depths_mm": depths,
            },
        )
        await self._activity(f"Rachio runtime refresh: {source}; {len(runtimes)} zones {runtimes}")

    # --- Home Assistant wiring ----------------------------------------------
    def async_start(self, hass: HomeAssistant) -> None:
        self._unsubs += [
            async_track_time_change(hass, self.irrigation_nightly,
                                    hour=23, minute=0, second=0),
            async_track_time_change(hass, self._on_calibrate_time,
                                    hour=6, minute=0, second=0),
            async_track_time_change(hass, self._on_settle_time,
                                    minute=[0, 30], second=0),
            async_at_started(hass, self._on_ha_started),
        ]

    async def _on_calibrate_time(self, _now) -> None:
        await self.irrigation_calibrate()

    async def _on_settle_time(self, _now) -> None:
        await self._settle_and_learn()

    async def _on_ha_started(self, _hass) -> None:
        self.startup_task = self._create_task(
            self._on_startup(), "geodrops_rachio_startup")

    async def async_shutdown(self) -> None:
        """Unsubscribe every trigger, cancel startup + run, and — the one behaviour
        change of the native port — stop the device if valves were watering.

        Idempotent: Task 12 may call it from both EVENT_HOMEASSISTANT_STOP and the
        entry unload; a second call finds nothing subscribed, no task and
        `_watering_active` already cleared by the run's `finally`.
        """
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        startup, self.startup_task = self.startup_task, None
        await _cancel_and_wait(startup)
        # Read BEFORE cancelling: the run's `finally` clears it while unwinding.
        was_watering = self._watering_active
        await self._cancel_run()
        if was_watering and self._current_cfg is not None:
            await self._safety_stop()

    async def _safety_stop(self) -> None:
        """Unload while valves were watering (spec: 'unload safety stop').

        Cancellation skips the runners' `except Exception` teardown, so a run
        cancelled mid-pause would leave Rachio to auto-resume the schedule with
        nobody watching. Stop the device and every zone, bounded.
        """
        bindings = self._current_bindings
        switches = [z.rachio_switch for z in self._current_cfg.zones.values()]
        try:
            async with asyncio.timeout(SAFETY_STOP_TIMEOUT_S):
                await self.port.call("rachio", "stop_watering",
                                     {"devices": bindings.rachio_device_name},
                                     blocking=True)
                for sw in switches:
                    await self.port.call("switch", "turn_off", {"entity_id": sw},
                                         blocking=True)
        except Exception as err:
            _LOGGER.warning(f"irrigation: unload safety stop failed ({err})")
