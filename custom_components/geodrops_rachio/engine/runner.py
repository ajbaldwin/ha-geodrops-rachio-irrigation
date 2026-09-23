"""Plan execution: hand Rachio its schedule, then watch it (poll-verify + aborts).

Ported from bundled_app/geodrops_rachio.py lines 70-113 (constants) and
522-938. Every sleep goes through the HAPort, so a test clock drives it.
"""
from __future__ import annotations

import logging

from ..brain import abort, blocks, program, recovery

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL_S = 30
# Consecutive polls showing nothing running before a block is called externally
# stopped. Rachio steps between zones inside a block on its own, and HA's view of
# both switches lags Rachio's cloud across that hand-off, so for a poll or two
# nothing reads `on`.
#
# Six polls = three minutes. It was two, and that ended three consecutive nights:
# on 2026-08-10 one zone's block completed at 04:16:38, the watch rendered its
# verdict the same second, and stop_all cut off the next zone three seconds after
# Rachio had legitimately started it. A hand-off is a coin flip against a 60 s
# threshold.
#
# The trade is deeply asymmetric and the original comment here had the reasoning
# but not the conclusion: a LATE verdict is free, because pyscript is not
# advancing zones during a block — Rachio owns the queue and we are only
# watching. An EARLY verdict costs the whole night plus a stop that truncates a
# healthy zone. So this wants to sit far on the tolerant side; three minutes is
# still far inside the 6-hour stuck-zone backstop.
EXTERNAL_STOP_POLLS = 6
# How long to let a finished block drain before moving on. Rachio's clock starts
# when it receives the call, a beat after ours, so the last zone can still be
# closing when our sleep ends.
BLOCK_DRAIN_TIMEOUT_S = 120
# Stop testing for an external stop this close to a block's scheduled end. The
# two clocks are independent: if Rachio's runs even slightly fast, the block
# finishes normally a poll or two before our sleep does, and without this the
# watch would read its own block completing as an external stop and abort the
# rest of the night. A real stop inside the grace window costs at most this much
# over-credited water on the block's last zone — a far cheaper error.
BLOCK_END_GRACE_S = 90
# How long a block gets to actually start watering. Past this with nothing ever
# seen on, the block did not start — something stopped it before our first poll
# caught it, which is what the adverse-conditions automation did on 2026-07-28.
# Without a bound, "not yet on" and "never came on" are indistinguishable and
# the second is scored as a full success.
BLOCK_START_CONFIRM_S = 90
# Post-resume probe window: how long to wait for a valve to come back on after
# a device resume before calling it a drop. Polled immediately, so a healthy
# resume returns at once; only a dropped schedule waits the full window.
# Matches BLOCK_START_CONFIRM_S so the probe can only ever accelerate a
# verdict the 90s never-started guard would also reach — never a shorter-
# window false positive on a healthy-but-slow resume. 90 is also a multiple
# of CHECK_INTERVAL_S (30), so the wait bound is exact.
RESUME_PROBE_S = 90


class RunnerMixin:
    # ─── Plan execution (poll-verify + abort watching) ───────────────────────

    def _abort_now(self, is_standby, is_manual_stop, is_rain):
        return abort.abort_reason(is_standby(), is_manual_stop(), is_rain())

    async def run_plan(self, slots, zone_switches, is_standby, is_manual_stop, is_rain,
                        is_rain_at_start):
        """Execute a plan block by block.

        A block is a maximal run of back-to-back watering slots; Rachio runs the
        whole block from one call while pyscript waits and watches. Idle soak slots
        are pyscript sleeping with nothing running. The happy path issues NO stop at
        all — the block simply ends when its last zone's minutes are up. Stops are
        reserved for aborts, which is both correct and the reason the operator gets
        a couple of Rachio notifications a night instead of dozens.
        """
        watered = []
        delivered = {}
        # What we actually asked Rachio for, block by block. Nothing else records
        # the durations that left this app, so without it the whole-minute
        # quantization is invisible after the fact.
        sent = []
        all_switches = list(zone_switches.values())
        try:
            for block in blocks.group_blocks(slots):
                reason = self._abort_now(is_standby, is_manual_stop, is_rain)
                if reason:
                    await self.stop_all(all_switches)
                    return {"watered": watered, "aborted_reason": reason,
                            "delivered_minutes": delivered, "blocks": sent}

                if block.kind == "idle":  # soak: nothing running
                    aborted, _idle = await self._sleep_watching(
                        block.minutes * 60, is_standby, is_manual_stop, is_rain
                    )
                    if aborted:
                        await self.stop_all(all_switches)
                        return {"watered": watered, "aborted_reason": aborted,
                                "delivered_minutes": delivered, "blocks": sent}
                    continue

                runs = blocks.quantize(block.slots)
                if not runs:
                    # Every slot rounded below a minute — nothing worth sending.
                    continue

                # Do not start INTO precipitation. Unlike the in-block watch, this
                # gate applies no sustain: the adverse-conditions automation aborts
                # the instant watering begins, so a block started now is a block
                # stopped now.
                if is_rain_at_start():
                    await self.stop_all(all_switches)
                    return {"watered": watered, "aborted_reason": "rain-at-start",
                            "delivered_minutes": delivered, "blocks": sent}

                # Poll-verify: nothing should be running before we hand over a
                # block. Unlike switch.turn_on, start_multiple_zone_schedule does
                # NOT stop current watering first, so an overlap would be ours to
                # cause. A stop here is an anomaly, not routine.
                for switch in zone_switches.values():
                    if self.poll_zone_running(switch):
                        _LOGGER.warning(
                            f"irrigation: {switch} was already running before a "
                            "block; stopping it first"
                        )
                        await self.stop_zone(switch)

                await self.start_block(runs, zone_switches)
                block_seconds = sum([r.minutes for r in runs]) * 60
                sent.append({
                    "minutes": int(block_seconds / 60),
                    "runs": [[r.zone_key, r.minutes] for r in runs],
                })
                # Watch that SOMETHING stays on rather than tracking which zone is
                # up: Rachio steps through the block itself, and everything that
                # stops us from outside — the adverse-conditions automation, the
                # stuck-zone automation, a manual stop in the app — goes through the
                # controller and stops all watering.
                aborted, elapsed = await self._sleep_watching(
                    block_seconds, is_standby, is_manual_stop, is_rain,
                    watch_switches=all_switches,
                )

                # Credit by measurement: the whole block when it ran to completion,
                # otherwise only the zones the elapsed time actually reached.
                gave = blocks.delivered(
                    runs, block_seconds if aborted is None else elapsed
                )
                for zone_key, minutes in gave.items():
                    delivered[zone_key] = delivered.get(zone_key, 0) + minutes
                    if zone_key not in watered:
                        watered.append(zone_key)

                if aborted:
                    if aborted == "external-stop":
                        _LOGGER.warning(
                            f"irrigation: watering stopped externally after "
                            f"{int(elapsed)}s of a {int(block_seconds / 60)} min "
                            "block; ending the run rather than starting more zones"
                        )
                    elif aborted == "never-started":
                        _LOGGER.warning(
                            f"irrigation: no zone came on within "
                            f"{BLOCK_START_CONFIRM_S}s of handing Rachio a "
                            f"{int(block_seconds / 60)} min block; something "
                            "stopped it at the start. Crediting no water and "
                            "ending the run rather than starting more zones"
                        )
                    await self.stop_all(all_switches)
                    return {"watered": watered, "aborted_reason": aborted,
                            "delivered_minutes": delivered, "blocks": sent}

                await self._await_block_end(all_switches)

            return {"watered": watered, "aborted_reason": None,
                    "delivered_minutes": delivered, "blocks": sent}
        except Exception:
            # Safety net: never leave a valve open if something unexpected raised.
            await self.stop_all(all_switches)
            raise

    def _crumb(self, crumbs, event, detail=None):
        """Append one timestamped execution breadcrumb.

        Persisted into the run record so a schedule drop is diagnosable from the
        record alone — the Rachio integration's own log was missing the morning the
        2026-08-27 drop had to be reconstructed from valve history.
        """
        entry = {"t": self.port.now().strftime("%H:%M:%S"),
                 "event": event}
        if detail is not None:
            entry["detail"] = detail
        crumbs.append(entry)

    async def run_collapsed(self, slots, zone_switches, is_standby, is_manual_stop, is_rain,
                            is_rain_at_start):
        """Execute a plan as ONE (or few) Rachio schedule(s) with device pauses.

        The whole night is flattened into a program (program.plan_program); water
        steps feed a single start_multiple_zone_schedule per segment, and pause steps
        become rachio.pause_watering/resume_watering that keep that one schedule alive
        across idle soak gaps. Result: one schedule-start notification per segment
        (one per night at the default unbounded budget) instead of one per block.

        Same return shape as run_plan. A persisted marker is set for the whole run so
        startup recovery can catch an interrupted (even paused) run; teardown is
        device-level (stop_device) so a collapsed schedule is killed outright.
        """
        tun = self._current_cfg.tunables
        steps = program.plan_program(slots)
        if not steps:
            return {"watered": [], "aborted_reason": None,
                    "delivered_minutes": {}, "blocks": [], "recoveries": 0,
                    "breadcrumbs": []}
        segments = program.segment_program(steps, tun.max_pauses_per_schedule)

        watered = []
        delivered = {}
        sent = []
        crumbs = []
        retries_used = 0
        recoveries = 0
        all_switches = list(zone_switches.values())
        switch_by_zone = zone_switches

        await self.set_run_active(True)
        try:
            for seg in segments:
                reason = self._abort_now(is_standby, is_manual_stop, is_rain)
                if reason:
                    await self.stop_device()
                    await self.stop_all(all_switches)
                    return {"watered": watered, "aborted_reason": reason,
                            "delivered_minutes": delivered, "blocks": sent,
                            "recoveries": recoveries, "breadcrumbs": crumbs}
                if is_rain_at_start():
                    await self.stop_device()
                    await self.stop_all(all_switches)
                    return {"watered": watered, "aborted_reason": "rain-at-start",
                            "delivered_minutes": delivered, "blocks": sent,
                            "recoveries": recoveries, "breadcrumbs": crumbs}

                steps = seg.steps
                is_recovery = False
                while True:
                    runs = program.program_runs(steps)
                    # Poll-verify nothing is already running before we hand over a
                    # schedule (start_multiple_zone_schedule does not stop current water).
                    for switch in all_switches:
                        if self.poll_zone_running(switch):
                            _LOGGER.warning(f"irrigation: {switch} was already running before "
                                        "a collapsed segment; stopping it first")
                            await self.stop_zone(switch)

                    self.api_calls += 1
                    await self.port.call(
                        "rachio", "start_multiple_zone_schedule", {
                            "entity_id": blocks.entity_ids(runs, switch_by_zone),
                            "duration": blocks.duration_csv(runs)})
                    sent.append({
                        "minutes": sum([r.minutes for r in runs]),
                        "runs": [[r.zone_key, r.minutes] for r in runs],
                    })
                    self._crumb(crumbs, "recover" if is_recovery else "start",
                               detail=[[r.zone_key, r.minutes] for r in runs])

                    aborted, watering_seconds, stopped_index = await self._walk_segment(
                        steps, all_switches, is_standby, is_manual_stop, is_rain,
                        crumbs)

                    # Credit by measurement; track what THIS schedule delivered so the
                    # recovery verdict can tell a dropped-mid-run schedule from one
                    # Rachio never started.
                    gave = blocks.delivered(runs, watering_seconds)
                    delivered_since_issue = 0
                    for zone_key, minutes in gave.items():
                        delivered[zone_key] = delivered.get(zone_key, 0) + minutes
                        delivered_since_issue += minutes
                        if zone_key not in watered:
                            watered.append(zone_key)

                    if aborted:
                        if aborted == "never-started":
                            self._crumb(crumbs, "drop-detected",
                                       detail=int(delivered_since_issue))
                        decision = recovery.verdict(
                            aborted, delivered_since_issue, is_recovery,
                            retries_used, tun.max_schedule_retries)
                        if decision == recovery.RECOVER:
                            _LOGGER.warning(
                                f"irrigation: Rachio dropped the schedule after "
                                f"{int(delivered_since_issue)} min; re-issuing for the "
                                f"remaining water (recovery {retries_used + 1})")
                            retries_used += 1
                            recoveries += 1
                            await self.stop_device()
                            steps = recovery.remaining_after(steps, stopped_index)
                            is_recovery = True
                            continue
                        # give-up or continue-abort: end the run.
                        self._crumb(crumbs, "give-up", detail=aborted)
                        await self.stop_device()
                        await self.stop_all(all_switches)
                        return {"watered": watered, "aborted_reason": aborted,
                                "delivered_minutes": delivered, "blocks": sent,
                                "recoveries": recoveries, "breadcrumbs": crumbs}
                    break  # segment completed cleanly

                await self._await_block_end(all_switches)

                if seg.gap_after > 0:  # between-segment idle (bounded fallback only)
                    idle_aborted, _e = await self._sleep_watching(
                        seg.gap_after * 60, is_standby, is_manual_stop, is_rain)
                    if idle_aborted:
                        await self.stop_device()
                        await self.stop_all(all_switches)
                        return {"watered": watered, "aborted_reason": idle_aborted,
                                "delivered_minutes": delivered, "blocks": sent,
                                "recoveries": recoveries, "breadcrumbs": crumbs}

            self._crumb(crumbs, "end")
            return {"watered": watered, "aborted_reason": None,
                    "delivered_minutes": delivered, "blocks": sent,
                    "recoveries": recoveries, "breadcrumbs": crumbs}
        except Exception:
            await self.stop_device()
            await self.stop_all(all_switches)
            raise
        finally:
            await self.set_run_active(False)

    async def _resume_took_hold(self, all_switches):
        """After a resume, did a valve actually come back on within the probe window?

        A dropped schedule resumes to nothing (a ~2 s valve blip was all 2026-08-27
        showed). Detecting that here front-runs the 90 s never-started guard and
        shortens the dead time before a re-issue; the guard in _sleep_watching stays
        as the backstop for a drop this short window misses.
        """
        waited = 0
        while True:
            if self.any_zone_running(all_switches):
                return True
            if waited >= RESUME_PROBE_S:
                return False
            await self.port.sleep(CHECK_INTERVAL_S)
            waited += CHECK_INTERVAL_S

    async def _walk_segment(self, steps, all_switches, is_standby, is_manual_stop, is_rain, crumbs):
        """Drive one schedule's steps; return (aborted, watering_seconds, stopped_index).

        watering_seconds counts only time under water steps (pauses excluded).
        stopped_index is the index into `steps` of the step the walk aborted on
        (len(steps) if it completed). After a resume, a post-resume probe checks the
        valve actually came back and reports a drop early (see _resume_took_hold);
        the _sleep_watching never-started guard is the backstop. Pause timing lands
        on our own clock (spec §3.4 residual).
        """
        watering_seconds = 0
        just_resumed = False
        for i, step in enumerate(steps):
            if step.kind == "water":
                if just_resumed and not await self._resume_took_hold(all_switches):
                    return "never-started", watering_seconds, i
                just_resumed = False
                aborted, elapsed = await self._sleep_watching(
                    step.minutes * 60, is_standby, is_manual_stop, is_rain,
                    watch_switches=all_switches,
                )
                watering_seconds += elapsed
                if aborted:
                    return aborted, watering_seconds, i
            else:  # pause: keep the schedule alive across an idle soak gap
                remaining = step.minutes
                aborted = None
                while remaining > 0:
                    # Clamp to [1, 60]: 60 is HA rachio.pause_watering's ceiling
                    # (pause_device clamps the same), so our sleep stays in lockstep
                    # with the device's auto-resume; max(1, ...) prevents a
                    # non-positive misconfig from stalling the loop.
                    span = max(1, min(60, self._current_cfg.tunables.max_pause_minutes, remaining))
                    await self.pause_device(span)
                    self._crumb(crumbs, "pause", detail=span)
                    aborted, _p = await self._sleep_watching(
                        span * 60, is_standby, is_manual_stop, is_rain,
                        watch_switches=all_switches, paused=True,
                    )
                    remaining -= span
                    if aborted:
                        return aborted, watering_seconds, i
                    if remaining > 0:
                        # chained pause: it auto-resumed; re-pause immediately.
                        await self.resume_device()
                        self._crumb(crumbs, "resume")
                await self.resume_device()
                self._crumb(crumbs, "resume")
                just_resumed = True
        return None, watering_seconds, len(steps)

    async def _await_block_end(self, zone_switches):
        """Wait for a finished block to actually close before moving on.

        Rachio's clock starts when it receives the call, a beat after ours starts,
        so the block's last zone can still be closing when our sleep ends. Stepping
        straight into the next block would make the pre-block poll-verify stop a
        zone that was about to finish on its own — cutting its tail short AND
        sending the operator a "stopped manually" notification for a run that was
        seconds from done.
        """
        waited = 0
        while waited < BLOCK_DRAIN_TIMEOUT_S:
            if not self.any_zone_running(zone_switches):
                return
            await self.port.sleep(CHECK_INTERVAL_S)
            waited += CHECK_INTERVAL_S
        _LOGGER.warning(
            f"irrigation: a zone was still running {waited}s after its block "
            "should have ended; stopping it"
        )
        await self.stop_all(zone_switches)

    async def _sleep_watching(self, seconds, is_standby, is_manual_stop, is_rain,
                              watch_switches=None, paused=False):
        """Sleep in CHECK_INTERVAL_S chunks while watching for aborts.

        Returns (reason, elapsed_seconds); reason is None when the full duration
        elapsed normally.

        When `watch_switches` is given, also verify that watering is still going.
        Plenty outside this scheduler can stop it: the HA "Detect Adverse Watering
        Conditions" automation, the stuck-zone automation, a manual stop in the
        Rachio app, or a controller hiccup. Ignoring that would credit water that
        was never delivered, so an external stop ends the run and reports the time
        actually watered.

        The verdict for each poll comes from `abort.watch_step`, which holds the
        three interacting guards (start confirmation, end grace, consecutive empty
        polls) as pure, unit-tested logic — they are where the subtle failures live.
        A block that never started reports zero elapsed: nothing was delivered, so
        nothing may be credited. An external stop reports the FIRST empty poll,
        where the water actually ended, not the poll that finally confirmed it.
        """
        elapsed = 0
        seen_on = False
        misses = 0
        stopped_at = 0
        while elapsed < seconds:
            chunk = min(CHECK_INTERVAL_S, seconds - elapsed)
            await self.port.sleep(chunk)
            elapsed += chunk
            reason = self._abort_now(is_standby, is_manual_stop, is_rain)
            if reason:
                return reason, elapsed
            if watch_switches:
                verdict, seen_on, new_misses = abort.watch_step(
                    self.any_zone_running(watch_switches), seen_on, misses,
                    elapsed, seconds,
                    BLOCK_START_CONFIRM_S, BLOCK_END_GRACE_S, EXTERNAL_STOP_POLLS,
                    paused=paused,
                )
                if misses == 0 and new_misses == 1:
                    stopped_at = elapsed
                misses = new_misses
                if verdict == "never-started":
                    return verdict, 0
                if verdict:
                    return verdict, stopped_at
        return None, elapsed
