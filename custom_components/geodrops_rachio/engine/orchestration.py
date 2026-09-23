"""The nightly run end to end: plan, wait, re-check, water, record, recap.

Ported from tests/legacy/geodrops_rachio_legacy.py lines 1574-1713 and 1755-2367.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..brain import (
    calibration, dosing, evaluate, plan, recovery, report_format, sensors)
from .store import WAITING_MARKER

_LOGGER = logging.getLogger(__name__)


class OrchestrationMixin:
    async def _write_waiting_marker(self, window_end_iso, stamp, trigger):
        """Persist that a run is WAITING for its pre-dawn window (see
        WAITING_MARKER_PATH). Written just before the wait sleep. A failure to
        persist must not take down the run — the wait still happens in memory; only
        restart recovery is forfeited, which is exactly today's behavior."""
        try:
            await self.store.write(
                WAITING_MARKER,
                {"window_end": window_end_iso, "stamp": stamp, "trigger": trigger},
            )
        except Exception as err:
            _LOGGER.warning(f"irrigation: could not persist waiting marker ({err})")

    async def _clear_waiting_marker(self):
        """Remove the waiting marker. Idempotent (see _delete_file); called both when
        the wait ends and after startup consumes it."""
        try:
            await self.store.delete(WAITING_MARKER)
        except Exception as err:
            _LOGGER.warning(f"irrigation: could not clear waiting marker ({err})")

    def _read_waiting_marker(self):
        """The parsed waiting marker, or None when absent/unreadable."""
        try:
            return self.store.read(WAITING_MARKER)
        except Exception as err:
            _LOGGER.warning(f"irrigation: could not read waiting marker ({err})")
            return None

    async def _publish_last_run(self, stamp, trigger, ctx=None, result=None, outcome=None,
                                skipped=None):
        """Full, untruncated record of what the nightly run actually did.

        The run used to leave behind one Logbook line and a recap; everything
        diagnostic lived in `pyscript.geodrops_rachio_preview`, written only by the
        preview service. So answering "did last night work?" meant cross-reading the
        Logbook, the recap, the system log and the Rachio app — and two open items
        (threshold calibration, quantized-vs-planned minutes) were simply
        unanswerable after the fact. This state is that record.
        """
        attributes = {
            "friendly_name": "Irrigation Last Run",
            "updated": stamp,
            # Which trigger produced this record. Without it the state is ambiguous:
            # a manual run and the nightly one look identical, and you cannot tell
            # which night you are reading.
            "trigger": trigger,
        }
        if skipped is not None:
            attributes["skipped"] = skipped
        if ctx is not None:
            pb, pb_instant = self._pressure_pair(ctx)
            the_plan = ctx["the_plan"]
            attributes.update({
                "drought_level": ctx["level"],
                "window_cap_hours": round(ctx["cap_minutes"] / 60.0, 2),
                "window_cap_source": ctx["cap_source"],
                "end_anchor": ctx["end_anchor"],
                "pressure_forecast": pb,
                "pressure_instant": pb_instant,
                "planned_zones": the_plan.watered,
                # Fractional, straight from the plan: compare against
                # delivered_minutes below to see the whole-minute quantization.
                "planned_minutes": {k: round(ctx["minutes"][k], 2)
                                    for k in the_plan.watered},
                "planned_span_minutes": the_plan.span_minutes,
                "runtime_sources": ctx["runtime_sources"],
                "horizon_hours": ctx["horizon_hours"],
            })
            doses = ctx["doses"]
            attributes["dosing_sources"] = ctx["dosing_sources"]
            dosing_detail = {}
            for k in the_plan.watered:
                d = doses[k]
                dosing_detail[k] = {
                    "deficit_pts": round(d.deficit_pts, 2),
                    "span_pts": round(d.span_pts, 2),
                    "frac": round(d.frac, 3),
                    "effective_depth_mm": round(d.effective_depth_mm, 2),
                    "scaled_full_refill_min": round(d.minutes / d.frac, 1) if d.frac > 0 else None,
                    "source": d.source,
                }
            attributes["dosing"] = dosing_detail
            cal_store = self._read_efficacy_store()
            calibration_detail = {}
            for k in the_plan.watered:
                crec = cal_store.get(k) or {}
                calibration_detail[k] = {
                    "state": crec.get("state", "calibrating"),
                    "efficacy": crec.get("efficacy"),
                    "span_pts": crec.get("span_pts"),
                    "n_obs": crec.get("n_obs", 0),
                    "last_reject_reason": crec.get("last_reject_reason"),
                }
            attributes["calibration"] = calibration_detail
            # Zones the window-start moisture re-check pulled from the plan (rain that
            # landed after planning). Absent when nothing was dropped.
            if ctx.get("window_start_dropped"):
                attributes["window_start_dropped"] = ctx["window_start_dropped"]
            if ctx.get("recovery_added"):
                attributes["recovery_added"] = ctx["recovery_added"]
        if result is not None:
            attributes.update({
                "start": result.start, "end": result.end,
                # Full tz-aware valve-close instant (empty on no-water nights). The
                # wrapper's Last Watered TIMESTAMP sensor parses this; `end` is the
                # time-only human string, which a TIMESTAMP sensor cannot read.
                "end_iso": result.end_iso,
                "window_start": result.window_start, "window_end": result.window_end,
                "watered": result.watered,
                "delivered_minutes": result.per_zone_minutes,
                "uncompleted": result.uncompleted,
            })
        if outcome is not None:
            # What was actually asked of Rachio, block by block — the only record of
            # the durations that left this app.
            attributes.update({
                "aborted_reason": outcome["aborted_reason"],
                "block_count": len(outcome.get("blocks", [])),
                "blocks": outcome.get("blocks", []),
                "recoveries": outcome.get("recoveries", 0),
                "breadcrumbs": outcome.get("breadcrumbs", []),
            })
        attributes["rachio_calls"] = self.api_calls
        attributes["state_polls"] = self.state_polls
        value = len(result.watered) if result is not None else 0
        self._publish("last_run", value, attributes)
        # The unattended run keeps its OWN copy. `last_run` is literally the last
        # one, so a manual re-run overwrites it — which is exactly what happened
        # while diagnosing the 2026-08-09 abort: the run_now erased the night we
        # were trying to read. A self-heal counts as the night's run; a run_now
        # does not.
        if trigger == "nightly" or trigger == "startup-heal":
            nightly = dict(attributes)
            nightly["friendly_name"] = "Irrigation Last Nightly Run"
            await self._publish_record("last_nightly", value, nightly)

    async def _plan_and_run(self, wait, trigger):
        """Plan and water. `wait=True` (nightly) sleeps until the pre-dawn window;
        `wait=False` (run-now) executes immediately."""
        self._manual_stop = False
        self._rain_since = None  # fresh sustain clock; a stale one could abort instantly
        self.reset_counters()
        stamp = self._naive_now().isoformat(timespec="seconds")
        self._set_status("planning")
        cfg = self._load_cfg()
        tun = cfg.tunables
        # _is_rain()/_is_standby() etc. read these globals rather than closing over
        # locals — pyscript lambdas/nested defs cannot see enclosing-function
        # locals, and several of these are also called as bare-reference callbacks.
        self._current_tun = tun
        self._current_cfg = cfg
        self._current_bindings = cfg.bindings
        self._run_in_progress = True
        # Keep the published target floors fresh each night (read-only; the floor
        # shifts with drought level between runs).
        await self._publish_targets(cfg)
        try:
            if self._is_standby():
                # Record the standby note at the END of the potential watering window,
                # not at the 23:00 trigger time. Otherwise the standby calendar entry
                # lands on the previous DAY relative to every normal recap (which is
                # written pre-dawn), making the two impossible to compare.
                event_time = None
                if wait:
                    try:
                        event_time = self._dawn_time() - dt.timedelta(
                            minutes=tun.end_offset_minutes
                        )
                    except Exception as err:
                        _LOGGER.warning(
                            f"irrigation: dawn unavailable for standby note ({err}); "
                            "recording at the current time"
                        )
                standby_result = report_format.RunResult(
                    [], {}, "", "", {}, standby=True
                )
                await self._publish_last_run(stamp, trigger, result=standby_result,
                                             skipped="standby")
                self._set_status("standby")
                await self._activity("Standby — nothing watered tonight")
                await self._report(standby_result, tun, event_time=event_time)
                return

            ctx = await self._plan_context(cfg)
            the_plan, start = ctx["the_plan"], ctx["start"]
            uncompleted, priority = ctx["uncompleted"], ctx["priority"]

            skip, skip_detail = self._rain_skip_check(ctx)
            if skip:
                summary = (
                    f"rain forecast ({int(skip_detail['probability_pct'])}%, "
                    f"{skip_detail['amount_mm']}mm / {skip_detail['horizon_hours']}h)"
                )
                if not wait:
                    # run_now expresses intent to water; the forecast informs, not vetoes.
                    _LOGGER.warning(f"irrigation: {summary} — watering anyway (manual run)")
                else:
                    for z in priority:
                        uncompleted[z] = "rain-forecast"
                    result = report_format.RunResult(
                        watered=[], uncompleted=uncompleted,
                        start="", end="", per_zone_minutes={}, standby=False,
                        window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
                        window_end=ctx["end"].astimezone().strftime("%H:%M"),
                        window_hours=round(ctx["cap_minutes"] / 60.0, 2),
                    )
                    await self._publish_last_run(stamp, trigger, ctx=ctx, result=result,
                                                 skipped="rain-forecast")
                    self._set_status("skipped", detail=summary)
                    await self._activity(f"Skipped: {summary}")
                    await self._report(result, tun, event_time=ctx["end"])
                    return

            if wait:
                wait_s = (start - self.port.now().astimezone(start.tzinfo)).total_seconds()
                if wait_s > 0:
                    start_str = start.astimezone().strftime("%H:%M")
                    self._set_status("waiting", detail=f"watering starts {start_str}")
                    await self._activity(
                        f"Plan ready — {len(the_plan.watered)} zone(s), "
                        f"{int(the_plan.span_minutes)} min span, starting {start_str}"
                    )
                    # Persist the wait so a restart during this multi-hour sleep can
                    # re-arm the run instead of losing the night silently. window_end
                    # is when the watering window closes (dawn − end_offset); startup
                    # re-arms only while now is still before it.
                    await self._write_waiting_marker(ctx["end"].isoformat(), stamp, trigger)
                    # Re-plan watch across the idle front: fold in a probe for any
                    # calibrating zone whose bad sensor recovers before the window
                    # closes. Adding a zone only ever moves `start` EARLIER (span
                    # grows), so the loop still converges to the run; fits_window
                    # keeps the enlarged run inside the window. No recovery -> this
                    # is a chunked no-op sleep, identical to a plain wait.
                    poll_s = tun.recovery_poll_seconds
                    watching = tun.self_calibration_enabled and bool(
                        ctx["recovery_candidates"])
                    pending_recovery = list(ctx["recovery_candidates"]) if watching else []
                    now_w = self.port.now().astimezone(start.tzinfo)
                    while now_w < start:
                        nap = (start - now_w).total_seconds()
                        if pending_recovery:
                            nap = min(nap, poll_s)
                        if nap > 0:
                            await self.port.sleep(nap)
                        now_w = self.port.now().astimezone(start.tzinfo)
                        if now_w >= start or not pending_recovery:
                            continue
                        rstore = self._read_efficacy_store()
                        still_pending = []
                        for rk in pending_recovery:
                            rzc = cfg.zones[rk]
                            rreading = sensors.read_zone(rzc, self._read_zone_signals(rzc))
                            if not rreading.online:
                                still_pending.append(rk)
                                continue
                            rrec = rstore.get(rk) or {}
                            rpinned = rzc.refill_span_pts > 0
                            if not calibration.should_probe(
                                    rrec.get("state", "calibrating"),
                                    rreading.dominant, rpinned, tun):
                                continue  # converged or above ceiling: nothing to gain
                            rbase = ctx["api_runtimes"].get(rzc.rachio_zone_id) or rzc.runtime_minutes
                            rfull = plan.cycles_minutes(rbase, 1.0)
                            rpm = calibration.probe_minutes(
                                rfull, rrec.get("prior_minutes"), rrec.get("last_rise"), tun)
                            rpm = calibration.cap_for_saturation(
                                rpm, rreading.dominant, rrec.get("efficacy"), tun)
                            rpm = min(rpm, rfull)
                            trial_minutes = dict(ctx["minutes"])
                            trial_minutes[rk] = rpm
                            trial_zones = list(priority) + [rk]
                            rgeo = {z: cfg.zones[z].geography for z in trial_zones}
                            radj = {z: cfg.zones[z].adjacency for z in trial_zones}
                            trial_plan = plan.build_plan(
                                trial_zones, trial_minutes, rgeo, radj,
                                ctx["cap_minutes"], tun)
                            if rk not in trial_plan.watered:
                                still_pending.append(rk)  # build_plan cap-trimmed it; window too tight now
                                continue
                            if not plan.fits_window(now_w, ctx["end"], trial_plan.span_minutes):
                                still_pending.append(rk)  # no room now; keep watching
                                continue
                            # Commit the probe into the live plan.
                            rdepth = ctx["api_depths"].get(rzc.rachio_zone_id) or rzc.refill_depth_mm
                            rfrac = min(rpm / rfull, 1.0) if rfull > 0 else 0.0
                            ctx["minutes"][rk] = rpm
                            ctx["doses"][rk] = dosing.DoseResult(
                                minutes=rpm, frac=rfrac,
                                effective_depth_mm=rfrac * float(rdepth),
                                deficit_pts=0.0, span_pts=0.0, source="probe")
                            ctx["dosing_sources"][rk] = "probe"
                            # The zone was skipped at plan time, so it has no entry in
                            # dominant_by_zone. The post-run pending-obs block reads
                            # pre_dominant from there; without this the settle poll
                            # sees pre_dominant=None and drops the obs (probe waters
                            # but never calibrates). Record the recovery reading as the
                            # pre-watering moisture.
                            ctx["dominant_by_zone"][rk] = rreading.dominant
                            priority.append(rk)
                            the_plan = trial_plan
                            ctx["the_plan"] = the_plan
                            start = ctx["end"] - dt.timedelta(minutes=the_plan.span_minutes)
                            ctx["start"] = start
                            ctx.setdefault("recovery_added", {})[rk] = {
                                "dominant": rreading.dominant}
                            await self._activity(
                                f"Recovery probe folded in: {rk} "
                                f"(dominant {rreading.dominant}, {int(rpm)} min)")
                            self._set_status(
                                "waiting",
                                detail=f"watering starts {start.astimezone():%H:%M}")
                        pending_recovery = still_pending
                        now_w = self.port.now().astimezone(start.tzinfo)
                    # The wait is over (whichever branch follows). Clear the marker so
                    # a later restart cannot re-arm a run that already left waiting.
                    await self._clear_waiting_marker()
                    # Re-establish this run's own bindings before reading them below.
                    # A preview fired during the wait save/restores these globals, but
                    # pyscript yields at every await inside preview — so if the sleep
                    # expired mid-preview, control could return here with preview's
                    # config still installed. Re-assigning synchronously (no await
                    # before the watering reads) makes the run own its context again.
                    self._current_cfg = cfg
                    self._current_bindings = cfg.bindings

                # The plan was built at 23:00 but watering starts hours later, and the
                # forecast refreshes every 15 minutes. Without this, a forecast that
                # turns bad after planning is ignored entirely — the in-run abort only
                # detects rain already falling.
                skip_now, skip_now_detail = self._rain_skip_check(ctx)
                if skip_now:
                    summary = (
                        f"rain forecast ({int(skip_now_detail['probability_pct'])}%, "
                        f"{skip_now_detail['amount_mm']}mm / "
                        f"{skip_now_detail['horizon_hours']}h)"
                    )
                    for z in priority:
                        uncompleted[z] = "rain-forecast"
                    result = report_format.RunResult(
                        watered=[], uncompleted=uncompleted,
                        start="", end="", per_zone_minutes={}, standby=False,
                        window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
                        window_end=ctx["end"].astimezone().strftime("%H:%M"),
                        window_hours=round(ctx["cap_minutes"] / 60.0, 2),
                    )
                    await self._publish_last_run(stamp, trigger, ctx=ctx, result=result,
                                                 skipped="rain-forecast-at-window-start")
                    self._set_status("skipped", detail=summary)
                    await self._activity(f"Skipped at window start: {summary}")
                    await self._report(result, tun, event_time=ctx["end"])
                    return

                # The plan's moisture readings were taken at ~23:00, but watering
                # starts hours later. Rain that lands in the gap can raise a zone's
                # moisture without being visible at plan time (GeoDrops sensors report
                # on a slow cadence). Re-read live dominant for each zone about to
                # water and drop any that no longer needs it — a calibration probe now
                # at/above the saturation ceiling, or a deficit zone now at/above its
                # floor. A dropped-to-empty plan takes the no-water path, like a rain
                # skip. Mirrors the rain re-check above.
                if the_plan.watered:
                    moisture_dropped = {}
                    for k in the_plan.watered:
                        zc = cfg.zones[k]
                        reading = sensors.read_zone(zc, self._read_zone_signals(zc))
                        reason = evaluate.revalidate_zone(
                            reading.online, reading.dominant,
                            ctx["dosing_sources"].get(k), ctx["floors"].get(k),
                            tun.probe_headroom_ceiling,
                        )
                        if reason is not None:
                            moisture_dropped[k] = {
                                "reason": reason, "dominant": reading.dominant,
                            }
                    if moisture_dropped:
                        for k in moisture_dropped:
                            uncompleted[k] = "moisture-risen"
                        ctx["window_start_dropped"] = moisture_dropped
                        survivors = [
                            z for z in priority
                            if z in the_plan.watered and z not in moisture_dropped
                        ]
                        geo = {z: cfg.zones[z].geography for z in survivors}
                        adjacency = {z: cfg.zones[z].adjacency for z in survivors}
                        surv_minutes = {z: ctx["minutes"][z] for z in survivors}
                        the_plan = plan.build_plan(
                            survivors, surv_minutes, geo, adjacency,
                            ctx["cap_minutes"], tun,
                        )
                        ctx["the_plan"] = the_plan
                        for z in the_plan.dropped:
                            uncompleted[z] = "insufficient window"
                        detail_bits = []
                        for k in moisture_dropped:
                            md = moisture_dropped[k]
                            detail_bits.append(
                                f"{k} ({md['reason']}, dominant {md['dominant']})"
                            )
                        await self._activity(
                            "Window-start re-check dropped: " + ", ".join(detail_bits)
                        )
                        if not the_plan.watered:
                            result = report_format.RunResult(
                                watered=[], uncompleted=uncompleted,
                                start="", end="", per_zone_minutes={}, standby=False,
                                window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
                                window_end=ctx["end"].astimezone().strftime("%H:%M"),
                                window_hours=round(ctx["cap_minutes"] / 60.0, 2),
                            )
                            await self._publish_last_run(stamp, trigger, ctx=ctx, result=result,
                                                         skipped="moisture-risen")
                            self._set_status("skipped", detail="soil already wet at window start")
                            await self._activity("Skipped at window start: soil already wet")
                            await self._report(result, tun, event_time=ctx["end"])
                            return

            zone_switches = {k: cfg.zones[k].rachio_switch for k in the_plan.watered}
            # When watering ACTUALLY begins, which is not the planned start. For a
            # nightly run the two coincide, because the plan's start is what we slept
            # until — but run_now does not sleep, so reporting the planned pre-dawn
            # start put the recap and the calendar entry 15 minutes adrift of the run
            # they describe. RunResult.start is documented as "when watering actually
            # began"; now it is.
            began = self.port.now()
            # From here until the finally, valves may open. A preview is refused for
            # this span (it would clobber the run-scoped globals a live run reads).
            self._watering_active = True
            self._set_status("watering", detail=f"{len(the_plan.watered)} zone(s)")
            if cfg.tunables.use_pause_collapse:
                outcome = await self.run_collapsed(
                    the_plan.slots, zone_switches,
                    self._is_standby, self._is_manual_stop, self._is_rain,
                    self._is_rain_at_start,
                )
            else:
                outcome = await self.run_plan(
                    the_plan.slots, zone_switches,
                    self._is_standby, self._is_manual_stop, self._is_rain,
                    self._is_rain_at_start,
                )

            watered = outcome["watered"]
            delivered = outcome.get("delivered_minutes", {})
            if outcome["aborted_reason"]:
                for z in priority:
                    if z not in watered and z not in uncompleted:
                        uncompleted[z] = outcome["aborted_reason"]

            end_anchor = ctx["end"]
            # Timezone-aware: `start` carries tzinfo, and the calendar entry compares
            # the two. A naive now() here would raise on that comparison.
            finished = self.port.now()
            result = report_format.RunResult(
                watered=watered, uncompleted=uncompleted,
                start=began.strftime("%H:%M"),
                end=finished.strftime("%H:%M"),
                # Full tz-aware valve-close instant for the wrapper's Last Watered
                # sensor; the time-only `end` above stays for human display.
                end_iso=finished.isoformat(),
                per_zone_minutes={k: delivered.get(k, 0) for k in watered}, standby=False,
                # The disease window that was available, reported separately from the
                # actual watering times (which collapse to a point when nothing ran).
                window_start=ctx["earliest_start"].astimezone().strftime("%H:%M"),
                window_end=end_anchor.astimezone().strftime("%H:%M"),
                window_hours=round(ctx["cap_minutes"] / 60.0, 2),
                recoveries=outcome.get("recoveries", 0),
            )
            # Record what happened BEFORE announcing it. The recap talks to notify and
            # calendar — external services whose failure modes this app does not own —
            # and when it went first, one rejected calendar event destroyed the entire
            # diagnostic trail: no irrigation_last_run, no per-zone entries, status
            # stranded on "watering", no "Run complete". The diagnostics exist for
            # exactly the nights that go wrong, so nothing fragile runs ahead of them.
            await self._publish_last_run(stamp, trigger, ctx=ctx, result=result,
                                         outcome=outcome)
            if tun.self_calibration_enabled and watered:
                pend = []
                for k in watered:
                    pend.append({
                        "zone": k,
                        "pre_dominant": ctx["dominant_by_zone"].get(k),
                        "minutes": delivered.get(k, 0),
                        "run_end_iso": finished.isoformat(),
                        # Accumulator (peak + retained) filled by the settle poll.
                        "peak": None,
                        "retained": None,
                        "last_seen_updated": None,
                    })
                await self._append_pending_obs(pend)
            await self._log_zone_outcomes(cfg, watered, delivered, uncompleted)
            aborted_reason = outcome["aborted_reason"]
            if aborted_reason:
                self._set_status("aborted", detail=aborted_reason)
            else:
                self._set_status("idle")
            await self._activity(
                f"Run complete — rachio_calls={self.api_calls}, state_polls={self.state_polls}"
            )
            # Anchor the recap to the end of the window so nightly entries always land
            # on the same date, whatever time the run actually finished. When water
            # actually ran, the entry SPANS the watering instead (see _report).
            await self._report(
                result, tun, event_time=end_anchor if wait else None,
                started_at=began if watered else None,
                ended_at=finished if watered else None,
            )
        finally:
            self._watering_active = False
            self._run_in_progress = False

    async def _preview(self):
        """Report the plan that WOULD run — no valves opened, no calendar written.

        Three surfaces (title 'Irrigation preview' on the notification carries the
        label, so message bodies never repeat it):
          - notify + Logbook: short human summary (Logbook truncates long text);
          - pyscript.geodrops_rachio_preview state (Developer Tools -> States): the FULL,
            untruncated breakdown incl. the dynamic-window characteristics.
        """
        # Only refuse while valves are actually watering — NOT during the pre-dawn
        # wait. A Planned/waiting night is exactly when "what would run tonight?"
        # is worth asking; blocking it there (the old _run_in_progress guard) left
        # preview silent for hours every night.
        if self._watering_active:
            msg = "Preview skipped — valves are watering right now."
            await self._notify(msg, "Irrigation Preview")
            await self._activity("Preview skipped: watering in progress")
            return
        stamp = self._naive_now().isoformat(timespec="seconds")
        cfg = self._load_cfg()
        # _is_standby, _plan_context's weather/forecast/sun reads and the notify
        # call all reach the loaded config through the run-scoped globals, so a
        # preview fired during a run's pre-dawn wait must not leave them mutated:
        # save, set, restore around the body. The waiting run ALSO re-establishes
        # its own bindings right after its sleep, closing the narrow case where the
        # sleep expires mid-preview (pyscript yields at every await in the body).
        _saved_cfg, _saved_bindings = self._current_cfg, self._current_bindings
        self._current_cfg = cfg
        self._current_bindings = cfg.bindings
        try:
            await self._preview_body(cfg, stamp)
        finally:
            self._current_cfg = _saved_cfg
            self._current_bindings = _saved_bindings

    async def _preview_body(self, cfg, stamp):
        """The preview computation itself, split out so _preview() can wrap it in a
        save/restore of the run-scoped globals (_current_cfg/_current_bindings) this
        body reads through _is_standby / _plan_context / _notify."""
        if self._is_standby():
            msg = "System in Standby — a run would water nothing."
            await self._notify(msg, "Irrigation Preview")
            await self._activity("Preview: " + msg)
            # Persisted so planned-runtime consumers survive a restart (see PERSISTED).
            await self._publish_record(
                "preview", "standby",
                {"updated": stamp, "standby": True, "message": msg},
            )
            return

        ctx = await self._plan_context(cfg)
        the_plan = ctx["the_plan"]
        wx = ctx["wx"]
        pb, pb_instant = self._pressure_pair(ctx)
        would_skip, skip_detail = self._rain_skip_check(ctx)
        cap_hours = round(ctx["cap_minutes"] / 60.0, 2)
        active = [name for name, on in
                  (("warm", pb["warm"]), ("humid", pb["humid"]), ("stagnant", pb["stagnant"]))
                  if on]
        active_str = ", ".join(active) if active else "none"
        start_str = ctx["start"].astimezone().strftime("%H:%M")
        end_str = ctx["end"].astimezone().strftime("%H:%M")
        earliest_str = ctx["earliest_start"].astimezone().strftime("%H:%M")
        span = int(the_plan.span_minutes)
        off = ctx["end_offset_minutes"]
        if off > 0:
            anchor_phrase = f"ends {off} min before {ctx['end_anchor']}"
        elif off < 0:
            anchor_phrase = f"ends {-off} min after {ctx['end_anchor']}"
        else:
            anchor_phrase = f"ends at {ctx['end_anchor']}"
        # The window is the disease window (earliest_start..end, cap hours long,
        # ending just before the end anchor) — independent of how many zones water.
        # The watering (span) is packed within it.
        window_note = (
            f"Window: {earliest_str}–{end_str} ({cap_hours}h, "
            f"pressure {pb['count']}/3: {active_str}), "
            f"{anchor_phrase}"
        )
        planned = [(z, ctx["minutes"][z]) for z in the_plan.watered]
        # Span is wall-clock (water + idle soaks); water_minutes is what is actually
        # delivered. They differ a lot for a lone zone, which must idle-soak 20 min
        # between its own cycles — reporting the span as "Watering" read as if it
        # contradicted the per-zone minutes.
        water_minutes = 0
        for _z, _m in planned:
            water_minutes += _m
        water_minutes = int(water_minutes)
        watering_note = (
            f"Runs {start_str}–{end_str} — {span} min span "
            f"({water_minutes} min water + soaks)"
        )

        msg = report_format.format_preview(
            ctx["level"], planned, ctx["uncompleted"],
            window_note=window_note, watering_note=watering_note,
        )
        # Make a silent fallback loud: if any planned zone used the static
        # config.yaml runtime, the Rachio pull failed for it and the plan is on
        # possibly-stale values.
        runtime_sources = ctx["runtime_sources"]
        static_zones = [k for k in the_plan.watered if runtime_sources.get(k) == "static"]
        if static_zones:
            msg = msg + (
                "\nNOTE: static fallback runtimes (Rachio pull failed) for: "
                + ", ".join(static_zones)
            )
        if would_skip:
            msg = msg + (
                f"\nWOULD SKIP: rain forecast "
                f"({int(skip_detail['probability_pct'])}%, {skip_detail['amount_mm']}mm"
                f" / {skip_detail['horizon_hours']}h)"
            )
        await self._notify(msg, "Irrigation Preview")
        await self._activity(
            f"Preview: would water {len(the_plan.watered)} zone(s), {water_minutes} min "
            f"water over a {span} min span, in window {earliest_str}-{end_str} "
            f"({cap_hours}h, pressure {pb['count']}/3)"
        )

        fwx = ctx["forecast_wx"]
        if fwx is None:
            forecast_weather = {}
        else:
            forecast_weather = {
                "overnight_temp_f": fwx.temp_f,
                "overnight_rh_pct": fwx.rh_pct,
                "overnight_wind_mph": fwx.wind_mph,
            }

        # Full, untruncated breakdown — Developer Tools -> States. Persisted (see
        # PERSISTED) so planned-runtime consumers keep a value across a restart.
        await self._publish_record(
            "preview",
            len(the_plan.watered),
            {
                "updated": stamp,
                "drought_level": ctx["level"],
                "window_cap_hours": cap_hours,
                "window_cap_source": ctx["cap_source"],
                "end_anchor": ctx["end_anchor"],
                "pressure_forecast": pb,
                "pressure_instant": pb_instant,
                "pressure_count": pb["count"],
                "pressure_active": active,
                "earliest_start": earliest_str,
                "start": start_str,
                "end": end_str,
                "span_minutes": span,          # wall-clock incl. idle soaks
                "water_minutes": water_minutes,  # actually delivered
                # Per planned zone: "live" (Rachio API) or "static" (config.yaml).
                "runtime_sources": runtime_sources,
                "runtimes_all_live": len(static_zones) == 0,
                "weather": {
                    "temp_f": wx.temp_f, "rh_pct": wx.rh_pct, "wind_mph": wx.wind_mph,
                    "dew_formed": wx.dew_formed, "precip_type": wx.precip_type,
                    "rain_last_hour_mm": wx.rain_last_hour_mm,
                },
                "planned_minutes": {z: int(m) for z, m in planned},
                "priority": ctx["priority"],
                "skipped": ctx["uncompleted"],
                "message": msg,
                "forecast_available": fwx is not None,
                "forecast_weather": forecast_weather,
                "rain_skip": would_skip,
                "rain_skip_detail": skip_detail,
            },
        )

    async def _log_zone_outcomes(self, cfg, watered, delivered, uncompleted):
        """File each zone's result under the ZONE, not just the run.

        This is what makes "what has this zone been doing?" a filter rather than a
        scroll: the entry lands in switch.<zone>'s own Logbook, next to the Rachio
        on/off events for the same night. Zones that did not water are logged too —
        the reason a zone was skipped is exactly what you go looking for later.
        """
        for zone_key in watered:
            zone = cfg.zones.get(zone_key)
            if zone is None:
                continue
            await self._activity(
                f"{report_format.display_name(zone_key)} watered "
                f"{round(delivered.get(zone_key, 0))} min",
                entity_id=zone.rachio_switch,
            )
        for zone_key, reason in uncompleted.items():
            zone = cfg.zones.get(zone_key)
            if zone is None:
                continue
            await self._activity(
                f"{report_format.display_name(zone_key)} not watered ({reason})",
                entity_id=zone.rachio_switch,
            )

    async def _report(self, result, tun, event_time=None, started_at=None, ended_at=None):
        """Notify + write the calendar entry.

        When water actually ran, `started_at`/`ended_at` make the entry SPAN the
        watering, so the calendar shows at a glance how long the system was out
        there. Otherwise — standby notes, rain skips, nothing watered — there is no
        interval to draw, and the entry is a 1-minute marker at `event_time`: the
        end-of-window anchor for nightly runs, so every nightly entry lands at the
        same point on the same date, or now for a run-now.

        Never zero-duration. HA's Local Calendar enforces a minimum of one second
        (`Expected minimum event duration of 0:00:01`), so a point-in-time entry is
        rejected 100% of the time on this backend. The old code asked for one
        anyway and fell back after catching the failure, which put a guaranteed
        warning in the system log every single night — in a design where the system
        log is supposed to carry errors ONLY, and noise in it means something is
        wrong.

        NOTE: a long window can start before midnight (the window opens up to 6 h
        before a ~04:55 dawn), in which case a spanning entry begins on the previous
        day. That is accurate rather than tidy, and only affects which day the entry
        sorts under.
        """
        msg = report_format.format_notification(result)
        await self._notify(msg, "Irrigation Recap")
        title, desc = report_format.format_calendar(
            result, tun.cycle_minutes, tun.soak_minutes
        )
        fallback = event_time if event_time is not None else self._naive_now()
        start_dt, end_dt = report_format.calendar_span(started_at, ended_at, fallback)
        # A calendar problem must cost the calendar entry and NOTHING else. This
        # call is the least important thing the run does and the most external:
        # it reaches a service whose validation this app does not control.
        try:
            await self.port.call(
                "calendar", "create_event", {
                    "entity_id": self._current_bindings.calendar_entity,
                    "summary": title, "description": desc,
                    "start_date_time": start_dt.isoformat(),
                    "end_date_time": end_dt.isoformat()},
            )
        except Exception as err:
            _LOGGER.warning(f"irrigation: calendar entry failed ({err}); recap was sent")
