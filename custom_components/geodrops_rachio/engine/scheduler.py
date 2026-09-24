"""The Scheduler: every engine mixin composed, plus triggers and lifecycle.

Ported from v0.9.15 bundled_app/geodrops_rachio.py lines 2461-2464 and 2604-2847
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
from .store import RUN_ACTIVE, RUN_PROGRESS

_LOGGER = logging.getLogger(__name__)

SAFETY_STOP_TIMEOUT_S = 10


async def _cancel_and_wait(task: asyncio.Task | None) -> None:
    """Cancel `task` and wait for it to unwind (its `finally` blocks run).

    The task's own CancelledError is absorbed — the caller asked for it — but a
    cancellation aimed at the CALLER while it waits is re-raised, never eaten.
    An ordinary exception raised while the task unwinds is not propagated (the
    task's done-callback logs it): the caller (reset / shutdown) still has
    teardown to do.
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
    except Exception:
        # Every task reaching here was started by Scheduler._spawn, whose
        # done-callback (_log_task_failure) already logs it with traceback.
        pass


def _log_task_failure(task: asyncio.Task) -> None:
    """Done-callback: log a crashed run/startup task at once, with traceback
    (pyscript logged a trigger's exception immediately). Retrieving it also
    stops asyncio's "Task exception was never retrieved" at GC time."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.error(f"irrigation: {task.get_name()} failed ({exc!r})", exc_info=exc)


class Scheduler(LearningMixin, OrchestrationMixin, PlanningMixin, RunnerMixin,
                IOMixin, EngineBase):
    def __init__(self, port, store, load_raw_config, fetch_zone_data,
                 create_task: Callable[[Coroutine, str], asyncio.Task]) -> None:
        super().__init__(port, store, load_raw_config, fetch_zone_data)
        self._create_task = create_task
        self.run_task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        # In-flight timed jobs (06:00 calibrate, :00/:30 settle), cancelled on
        # shutdown like the run and startup tasks.
        self._jobs: set[asyncio.Task] = set()
        self._unsubs: list[Callable[[], None]] = []
        # Serialises every start/cancel of the run task, and counts requests so
        # the LAST caller wins (pyscript's task.unique is synchronous: whoever
        # calls it last is the one survivor).
        self._run_lock = asyncio.Lock()
        self._run_gen = 0

    # --- the one run task (replaces task.unique("geodrops_rachio_run")) -------
    async def _cancel_run(self) -> None:
        """Cancel the run task — and supersede any _start_run still in flight —
        leaving `self.run_task` None."""
        self._run_gen += 1
        async with self._run_lock:
            await self._cancel_current_run()

    async def _cancel_current_run(self) -> None:
        # Caller holds _run_lock.
        task, self.run_task = self.run_task, None
        await _cancel_and_wait(task)

    async def _start_run(self, wait: bool, trigger: str, resume: dict | None = None) -> None:
        """Replace any run with a fresh `_plan_and_run(wait, trigger)` (finishing
        an interrupted night's `resume` minutes, when given).

        Under the lock, so the old run finishes unwinding (its `finally` clears
        the run flags and the run-active marker) BEFORE the new one starts, and
        two overlapping starts can never both leave a live task. A start that a
        later start/cancel superseded while it waited starts nothing.
        """
        self._run_gen += 1
        gen = self._run_gen
        async with self._run_lock:
            if gen != self._run_gen:
                return
            await self._cancel_current_run()
            if gen != self._run_gen:
                return
            run = (self._plan_and_run(wait, trigger) if resume is None
                   else self._plan_and_run(wait, trigger, resume=resume))
            self.run_task = self._spawn(run, "geodrops_rachio_run")

    def _spawn(self, coro: Coroutine, name: str) -> asyncio.Task:
        task = self._create_task(coro, name)
        task.add_done_callback(_log_task_failure)
        return task

    # --- triggers ---------------------------------------------------------
    async def irrigation_nightly(self, _now=None) -> None:
        await self._start_run(True, "nightly")

    # --- ported: legacy lines 2604-2742 ---------------------------------------
    async def _on_startup(self):
        """Safety net after any HA restart or integration reload.

        A run interrupted mid-watering can leave a Rachio valve OPEN: a crash or OOM
        kill gives the engine no chance to stop the zone (a graceful stop or unload
        does — see async_shutdown), and no recap is sent. Without this, the only
        backstop is the 6-hour stuck-zone automation. On startup we poll the managed
        zones and close anything still running, noting it in the Logbook. A short
        sleep first lets the Rachio integration load its switch entities before we
        poll them.

        A run that got as far as watering leaves its progress in the Store (the plan
        and each step counted as delivered as it starts), whether HA crashed or
        stopped gracefully. That is checked first: inside the watering window, only
        what is still owed is watered (never re-planned from moisture, which lags the
        watering); after it, the night is recorded as interrupted. The checks below
        are the fallback for an interruption with no progress recorded.

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
        # Status is not persisted, so it would read `unknown` until the next run.
        # Publish it now so a filtered Logbook and any dashboard card have
        # something to point at from the moment HA comes back.
        self._set_status("idle")
        # Bring back last night's record and the calibration series from the Store,
        # so a restart does not erase the evidence for the very night someone is
        # about to ask about.
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

        marker_set = bool(self.store.read(RUN_ACTIVE))

        running = []
        for zone in cfg.zones.values():
            try:
                if self.poll_zone_running(zone.rachio_switch):
                    running.append(zone.rachio_switch)
            except Exception:
                pass

        # A night's persisted progress is the precise record: resume what it still
        # owes, or record it — never re-plan from moisture that lags the watering.
        # Without one (e.g. the night of an upgrade), fall through to the checks
        # below, unchanged.
        progress = self.store.read(RUN_PROGRESS)
        if progress is not None:
            if await self._handle_progress(progress, marker_set, running):
                return

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
            action = recovery.startup_action(marker, self.port.now().isoformat())
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

    async def _handle_progress(self, progress, marker_set, running) -> bool:
        """Act on an interrupted night's progress; True if startup is done."""
        action, owed = recovery.resume_action(progress, self.port.now().isoformat())
        if action == recovery.IGNORE:
            await self.store.write(RUN_PROGRESS, None)
            return False
        if marker_set or running:
            # Kill any running or about-to-auto-resume schedule outright.
            await self.stop_device()
            if running:
                await self.stop_all(running)
            await self.set_run_active(False)
        await self._clear_waiting_marker()
        if action == recovery.RESUME:
            owed_txt = ", ".join(f"{z} {round(m, 1)} min" for z, m in owed.items())
            await self._activity(
                f"Startup: resuming the run HA interrupted; still owed: {owed_txt}")
            # The progress stays until the resumed run opens its own (a restart
            # before it starts watering resumes again).
            await self._start_run(True, "startup-resume", resume=owed)
            return True
        # INTERRUPTED: the window has closed (or nothing is owed). Record it.
        stamp = progress.get("stamp") or self._naive_now().isoformat(timespec="seconds")
        await self._publish_last_run(stamp, progress.get("trigger") or "nightly",
                                     skipped="interrupted-restart")
        self._set_status("skipped", detail="interrupted — HA restarted mid-run")
        await self._activity(
            "Startup: a run was interrupted by an HA restart and the watering window "
            "has closed; recorded as interrupted")
        await self.store.write(RUN_PROGRESS, None)
        return True

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
        open valves, clears the waiting + run-active markers and the night's run
        progress, and returns status to idle. Safe to call in any state.
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
            # A reset night is over: nothing may resume it after a restart.
            await self.store.write(RUN_PROGRESS, None)
        except Exception as err:
            _LOGGER.warning(f"irrigation: reset — marker cleanup skipped: {err}")
        self._set_status("idle", detail="reset")
        await self._activity("Manual reset — planned/running run cancelled; system idle")

    # --- ported: legacy lines 2794-2834 ---------------------------------------
    async def async_refresh_runtimes(self):
        """Force a live Rachio runtime fetch and record the result — checkable
        without the system log:
          1. Logbook entry named "Irrigation" (Settings -> Logbook / activity log);
          2. the `runtimes` record — value is the zone count; source/live/updated/
             runtimes_minutes/refill_depths_mm are attributes. Not an entity of its
             own: the zone sensors read their live refill depth from it.
        A live pull yields a non-empty dict keyed by rachio_zone_id; an empty dict
        means the fetch failed and the scheduler is on the zones' configured runtimes.
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
        self._spawn_job(self.irrigation_calibrate(), "geodrops_rachio_calibrate")

    async def _on_settle_time(self, _now) -> None:
        self._spawn_job(self._settle_and_learn(), "geodrops_rachio_settle")

    def _spawn_job(self, coro: Coroutine, name: str) -> None:
        task = self._spawn(coro, name)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _on_ha_started(self, _hass) -> None:
        self.startup_task = self._spawn(
            self._on_startup(), "geodrops_rachio_startup")

    async def async_shutdown(self) -> None:
        """Unsubscribe every trigger, cancel startup, timed jobs + run, and — the one behaviour
        change of the native port — stop the device if valves were watering.

        Idempotent: it runs as an HA stage-1 shutdown job AND on entry unload; a
        second call finds nothing subscribed, no task and `_watering_active`
        already cleared by the run's `finally`.
        """
        # Read BEFORE ANY await: the run's `finally` clears it while unwinding,
        # and the run may already be cancelled (HA cancels background tasks at
        # stop) — any yield below lets that unwinding happen first.
        was_watering = self._watering_active
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        startup, self.startup_task = self.startup_task, None
        await _cancel_and_wait(startup)
        for job in list(self._jobs):
            await _cancel_and_wait(job)
        # A run the startup heal began watering while we waited counts too.
        was_watering = was_watering or self._watering_active
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
