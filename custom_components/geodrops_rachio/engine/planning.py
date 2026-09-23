"""Planning: abort predicates, sensor/weather/sun reads, and the nightly plan.

Ported from bundled_app/geodrops_rachio.py lines 941-1532 and 1716-1752.
"""
from __future__ import annotations

import datetime as dt
import logging
import random

from ..brain import (
    calibration, config, dosing, drought, evaluate, plan, sensors, weather)

_LOGGER = logging.getLogger(__name__)

# Offline reasons that mean the SENSOR was unusable at plan time (as opposed to
# "excluded" or being above target). A calibrating zone skipped for one of these
# is a sensor-recovery probe candidate. Mirrors sensors.read_zone.
SENSOR_SKIP_REASONS = frozenset(("unavailable", "low_quality"))


class PlanningMixin:
    # ─── Orchestration ───────────────────────────────────────────────────────

    def _zone_excluded(self, zone):
        """True only when the zone's exclude_boolean is explicitly "on".

        Empty binding, or a missing/unavailable/renamed entity, fails safe to
        NOT excluded (normal watering) — same NameError-tolerant pattern as
        _is_standby. A zone is excluded (from both the nightly plan and probing)
        only when its mapped helper reads "on".

        Edge: a momentary "unavailable" at plan time (e.g. an HA restart racing the
        23:00 plan) reads as not-excluded for that one cycle, looking like a
        return-from-exclusion. Benign — a long exclusion then recalibrates (the
        desired end state anyway) and a short one stays under the threshold and keeps
        its calibration.
        """
        ent = zone.exclude_boolean
        if not ent:
            return False
        return self.port.state(ent) == "on"

    def _is_standby(self):
        # Either standby source disables the system: the Rachio-native switch OR the
        # input_boolean.irrigation_standby helper (the user-facing disable toggle).
        # Reads _current_bindings (module global, not a param) for the same reason
        # _is_rain() reads _current_tun: this is passed by reference as a callback
        # and pyscript cannot close over enclosing locals.
        #
        # Each read is NameError-tolerant, same pattern as the spray-switch loop in
        # _rain_condition_now(): a bad/renamed standby entity id must degrade to
        # "not in standby" rather than crash the watch loop.
        standby = False
        if self.port.state(self._current_bindings.standby_switch) == "on":
            standby = True
        if self.port.state(self._current_bindings.standby_boolean) == "on":
            standby = True
        return standby

    def _is_manual_stop(self):
        return self._manual_stop

    def _is_rain(self):
        """Rain-abort predicate for run_plan, structured to match the HA automation
        "Irrigation - Detect Adverse Watering Conditions" so the two do not fight
        over the valves. NB the spray-zone rule diverged on 2026-08-11: this now
        corroborates with the rain gauge, while the automation still uses RH >= 92.
        Until that automation is switched to the gauge too, it can still stop a run
        on a spray-flagged zone's own spray (an `external-stop`) that this predicate
        no longer triggers.

          - hail / rain_hail aborts IMMEDIATELY (that automation trigger has no
            `for:` delay);
          - plain rain must persist `rain_sustain_seconds` (its 2m30s `for:`) before
            aborting, so a momentary misread does not kill a run;
          - while any zone flagged `spray: true` in config (config.spray_switches)
            is watering, "rain" is only believed if it is hail or the rain gauge
            shows accumulation — the Tempest misreads such a zone's overspray as
            "rain" while the gauge stays at 0, so without this the system aborts
            on its own sprinkler.

        Module-level (reading the _current_tun/_current_cfg globals) rather than a
        closure: pyscript lambdas and nested defs do NOT capture enclosing-function
        locals.
        """
        holds, hail = self._rain_condition_now()
        abort_now, self._rain_since = weather.rain_sustain_step(
            holds, hail, self._rain_since, self.port.now().timestamp(),
            self._current_tun.rain_sustain_seconds if self._current_tun else 0,
        )
        return abort_now

    def _rain_condition_now(self):
        """(condition_holds, is_hail) for the automation's rule, right now.

        The single reading both rain gates share, so the "is it raining" question
        cannot drift between them — they differ ONLY in whether a sustain applies.
        """
        if self._current_tun is None or self._current_cfg is None:
            return False, False
        wx = self._read_weather(self._current_tun)
        # Explicit loop (no genexpr/any() closure — pyscript does not implement
        # generator expressions, and a lambda/nested-def here could not capture
        # `spray_on` from the enclosing scope anyway). Any flagged spray zone
        # corroborating with the gauge is enough to treat "rain" as its overspray.
        spray_on = False
        for sw in config.spray_switches(self._current_cfg):
            if self.port.state(sw) == "on":
                spray_on = True
        return weather.is_rain_abort(wx, self._current_tun, spray_on), weather.is_hail(wx)

    def _is_rain_at_start(self):
        """Rain gate for the moment a block is about to begin — NO sustain.

        `_is_rain()` cannot return True on its first evaluation: with no history it
        starts the sustain clock and defers. That is right for interrupting a
        running block and wrong here. The automation this mirrors makes the same
        distinction — its `rain_sustained` trigger waits 2m30s, but
        `irrigation_started` fires the instant watering begins, with no delay, and
        with any zone other than the spray-flagged one running its condition reduces
        to bare `precip`. On 2026-07-28 both facts collided: our gate deferred on a
        momentary reading, the block launched, and the automation stopped it
        seconds later.

        At the start there is nothing to protect. Waiting costs nothing; starting
        costs a Rachio call, a push notification, and a fight with an automation
        that has zero tolerance.
        """
        holds, _hail = self._rain_condition_now()
        return holds

    def _read_weather(self, tun):
        # Weather only sizes the disease window; a missing/unavailable sensor must
        # degrade to a default, never crash the run. pyscript's state.get raises
        # NameError when an entity does not exist at all (distinct from a present-
        # but-"unavailable" value that float() rejects), so catch NameError too and
        # warn once per missing entity (a wrong/absent entity id is a misconfig).
        def num(entity, default=0.0):
            raw = self.port.state(entity)
            if raw is None:
                _LOGGER.warning(f"irrigation: weather entity {entity!r} not found; using {default}")
                return default
            try:
                return float(raw)
            except (ValueError, TypeError):
                return default

        def text(entity, default=""):
            raw = self.port.state(entity)
            if raw is None:
                _LOGGER.warning(f"irrigation: weather entity {entity!r} not found; using {default!r}")
                return default
            return raw

        wx_ids = self._current_bindings.weather
        return weather.WeatherReading(
            temp_f=num(wx_ids.temperature),
            rh_pct=num(wx_ids.humidity),
            # Averaged local wind smooths momentary lulls that would otherwise
            # falsely trip the "stagnant" disease signal on a once-per-night read.
            wind_mph=num(wx_ids.wind),
            dew_formed=text(self._current_bindings.dew_formed_boolean) == "on",
            rain_last_hour_mm=num(wx_ids.rain_last_hour),
            precip_type=text(wx_ids.precip_type) or "none",
        )

    def _forecast_num(self, entity):
        """Read a numeric forecast sensor. Returns None when unusable.

        Forecast data is an optimisation, never a prerequisite: a missing entity
        (state.get raises NameError — gotcha #10) or an `unknown` / `unavailable`
        value returns None so the caller falls back. Warnings go to the SYSTEM log
        because an absent forecast sensor is a misconfiguration or outage, not
        routine operation.
        """
        raw = self.port.state(entity)
        if raw is None:
            _LOGGER.warning(f"irrigation: forecast entity {entity!r} not found")
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            _LOGGER.warning(f"irrigation: forecast entity {entity!r} unusable ({raw!r})")
            return None

    def _read_forecast_weather(self):
        """A WeatherReading describing the OVERNIGHT hours, for the window cap only.

        Returns None if any component is unavailable, so the caller falls back to
        instantaneous readings.

        dew_formed is deliberately False: `input_boolean.dew_formed` is a CURRENT
        observation, and using it to describe 00:00-05:00 is exactly the staleness
        this replaces. Sustained overnight RH at or above humid_rh_pct already
        captures dew conditions.

        The precipitation fields are zeroed: this reading sizes the disease window
        only. Rain ABORT during a run stays instantaneous (_read_weather) and still
        mirrors the HA adverse-conditions automation.
        """
        d = self._current_bindings.derived.forecast_overnight
        temp_f = self._forecast_num(d["temp"])
        rh_pct = self._forecast_num(d["humidity"])
        wind_mph = self._forecast_num(d["wind"])
        if temp_f is None or rh_pct is None or wind_mph is None:
            return None
        return weather.WeatherReading(
            temp_f=temp_f, rh_pct=rh_pct, wind_mph=wind_mph,
            dew_formed=False, rain_last_hour_mm=0.0, precip_type="none",
        )

    def _read_observed_overnight(self):
        """WeatherReading from the OBSERVED overnight means, or None if unusable.

        Deliberately mirrors _read_forecast_overnight: precipitation fields zeroed
        and dew_formed False, so the two readings differ only in whether the numbers
        were predicted or measured. Anything else would compare two models rather
        than one model against reality.
        """
        d = self._current_bindings.derived.observed_overnight
        temp_f = self._forecast_num(d["temp"])
        rh_pct = self._forecast_num(d["humidity"])
        wind_mph = self._forecast_num(d["wind"])
        if temp_f is None or rh_pct is None or wind_mph is None:
            return None
        return weather.WeatherReading(
            temp_f=temp_f, rh_pct=rh_pct, wind_mph=wind_mph,
            dew_formed=False, rain_last_hour_mm=0.0, precip_type="none",
        )

    def _read_forecast_precip(self, horizon_hours):
        """(probability_pct, amount_mm) for the given horizon; (None, None) if
        unavailable. The horizon comes from the active drought profile, and Task 1
        publishes sensors for 12/18/24 h."""
        d = self._current_bindings.derived
        prob = self._forecast_num(f"{d.precipitation_chance_prefix}{horizon_hours}_hour")
        amount = self._forecast_num(f"{d.precipitation_amount_prefix}{horizon_hours}_hour")
        return prob, amount

    def _read_zone_signals(self, zone):
        return sensors.ZoneSignals(
            dominant=self._state_get(zone.dominant_sensor),
            state=self._state_get(zone.state_sensor),
            # pyscript does not implement generator expressions (ast_generatorexp);
            # a list comprehension is supported and equivalent here.
            qualities=tuple([self._state_get(q) for q in zone.quality_sensors]),
        )

    def _sensor_last_updated(self, entity):
        """The HA `last_updated` (tz-aware UTC datetime) of a state entity, or None.

        pyscript exposes it as a virtual attribute via state.get("<entity>.last_updated").
        We use last_updated (bumps only when the value changes) — NOT last_reported
        (bumps on every MQTT republish, which would defeat the freshness gate). Any
        failure (missing entity -> NameError, or unexpected type) yields None, which
        settle_decision treats as "not fresh".
        """
        try:
            return self._last_updated_get(entity)
        except Exception:
            return None

    def _dawn_time(self):
        return dt.datetime.fromisoformat(self._state_get(self._current_bindings.sun.dawn))

    def _end_anchor_time(self, anchor):
        """When the watering window must finish, for the given anchor.

        `anchor` is a drought profile's `end_anchor` ("dawn" | "sunrise"). Falls back
        to dawn when that anchor's sensor does not exist (state.get raises NameError
        for a nonexistent entity — gotcha #10), because dawn is the earlier of the
        two: a lookup failure must shorten the window, never extend watering past
        sunrise.
        """
        entity = (
            self._current_bindings.sun.sunrise if anchor == "sunrise"
            else self._current_bindings.sun.dawn
        )
        raw = self.port.state(entity)
        if raw is None:
            _LOGGER.warning(
                f"irrigation: end-anchor entity {entity!r} not found; using dawn"
            )
            return self._dawn_time()
        return dt.datetime.fromisoformat(raw)

    def _resolve_profile(self, cfg):
        """(level, profile) for the current drought selection.

        pyscript's state.get raises NameError when the entity does not exist at all
        (vs. returning "unavailable" for a present-but-unknown entity). Treat a
        missing helper the same as an unknown value: fall back to Level 3 - Critical
        and warn to the system log — a missing helper is a misconfiguration, not
        routine operation. Deliberately NOT Level 4 - Emergency: that never waters,
        so a typo in the helper's options would silently let the lawn die. Level 3
        is the least-water profile that still waters.
        """
        level = self.port.state(cfg.bindings.drought_level_select)
        profile = cfg.drought_profiles.get(level)
        if profile is None:
            _LOGGER.warning(
                f"irrigation: drought level {level!r} missing or unknown; "
                "defaulting to Level 3 - Critical"
            )
            profile = cfg.drought_profiles["Level 3 - Critical"]
            level = "Level 3 - Critical"
        return level, profile

    def _compute_target_floors(self, cfg):
        """{zone_key: effective target floor} for online, non-excluded zones.

        READ-ONLY, unlike _plan_context: it evaluates targets but never touches the
        efficacy store (no exclusion stamping / recalibrate-on-return) and never
        plans or waters. That is what makes it safe to call at startup or on demand
        to hydrate the target-floor state without perturbing calibration.
        """
        _level, profile = self._resolve_profile(cfg)
        floors = {}
        for key, zone in cfg.zones.items():
            if self._zone_excluded(zone):
                continue
            reading = sensors.read_zone(zone, self._read_zone_signals(zone))
            if not reading.online:
                continue
            floors[key] = round(drought.effective_target(zone, cfg.bands, profile).floor, 1)
        return floors

    async def _publish_targets(self, cfg):
        """Publish per-zone target floors to pyscript.geodrops_rachio_targets (persisted),
        so a Deficit sensor has a target the moment HA restarts — before the first
        nightly plan. Refreshed at startup and each nightly. Read-only (see
        _compute_target_floors)."""
        floors = self._compute_target_floors(cfg)
        await self._publish_record("targets", len(floors), {
            "friendly_name": "Irrigation Target Floors",
            "updated": self._naive_now().isoformat(timespec="seconds"),
            "target_floors": floors,
        })

    async def _plan_context(self, cfg):
        """Evaluate zones and build the full plan — no execution. Shared by the
        real run and the preview so both see identical decisions."""
        tun = cfg.tunables
        level, profile = self._resolve_profile(cfg)
        rng = random.Random()

        # Loaded before the zone loop so exclusion handling can stamp/reset it; the
        # dose loop and probe injection below reuse this same (possibly updated) store.
        efficacy_store = self._read_efficacy_store()
        store_dirty = False
        now = self.port.now()
        evals, targets, uncompleted, dominant_by_zone = [], {}, {}, {}
        for key, zone in cfg.zones.items():
            rec = efficacy_store.get(key)
            if self._zone_excluded(zone):
                # Excluded (e.g. an overseeded zone watered separately): skip the
                # nightly plan AND probing. Stamp the first excluded plan so a later
                # return can time-gate whether to recalibrate.
                if rec is None:
                    rec = {}
                if "excluded_since" not in rec:
                    rec["excluded_since"] = now.isoformat()
                    efficacy_store[key] = rec
                    store_dirty = True
                uncompleted[key] = "excluded"
                continue
            if rec is not None and rec.get("excluded_since") is not None:
                # Returned from exclusion: reset to recalibrating if it was out long
                # enough (soil likely changed, e.g. overseed), else just clear the stamp.
                efficacy_store[key] = calibration.exclusion_return(
                    rec, now, tun.recalibrate_after_exclusion_hours)
                store_dirty = True
            reading = sensors.read_zone(zone, self._read_zone_signals(zone))
            if not reading.online:
                uncompleted[key] = reading.offline_reason
                continue
            tgt = drought.effective_target(zone, cfg.bands, profile)
            targets[key] = tgt
            dominant_by_zone[key] = reading.dominant
            evals.append(evaluate.evaluate_zone(reading, tgt))
        if store_dirty:
            await self._write_efficacy_store(efficacy_store)

        priority = [e.key for e in evaluate.sort_by_priority(evals, rng)]

        # Per-zone full-refill runtime: prefer a live Rachio pull (keyed by
        # rachio_zone_id), else the static runtime_minutes from config.yaml.
        # Prefer the live Rachio runtime (keyed by rachio_zone_id), else the static
        # runtime_minutes from config.yaml. Record which source each zone used: the
        # fallback is otherwise SILENT, so a mid-season Rachio outage would quietly
        # plan on stale values with nothing to show for it.
        api_runtimes = await self.get_runtimes()
        api_spans = await self.get_refill_spans()
        api_depths_for_dose = await self.get_refill_depths()
        # True active probing: online, untriggered, calibrating/recalibrating zones
        # with headroom get a probe run appended at the TAIL of priority (lowest
        # priority — build_plan drops these first under a tight window, so they never
        # crowd out a triggered zone). `targets` holds every online zone; those in
        # `priority` are already triggered. Self-limiting: a zone stops being a
        # candidate once should_probe returns False (converged, or dominant >= ceiling).
        if tun.self_calibration_enabled:
            triggered = set(priority)
            for key in targets:
                if key in triggered:
                    continue
                rec = efficacy_store.get(key) or {}
                pinned = cfg.zones[key].refill_span_pts > 0
                if calibration.should_probe(rec.get("state", "calibrating"),
                                            dominant_by_zone[key], pinned, tun):
                    priority.append(key)
        minutes = {}
        runtime_sources = {}
        doses = {}
        dosing_sources = {}
        for k in priority:
            zone_cfg = cfg.zones[k]
            live = api_runtimes.get(zone_cfg.rachio_zone_id)
            if live:
                base = live
                runtime_sources[k] = "live"
            else:
                base = zone_cfg.runtime_minutes
                runtime_sources[k] = "static"
            # Resolve span: config pin -> learned -> live Rachio -> None (fallback).
            # A set refill_span_pts PINS the zone (wins over learning). A learned span
            # is used unless the zone is recalibrating (post-swap, efficacy invalid).
            live_span = api_spans.get(zone_cfg.rachio_zone_id)
            eff = efficacy_store.get(k)
            if zone_cfg.refill_span_pts > 0:
                span_pts = zone_cfg.refill_span_pts
                span_source = "config"
            elif eff and eff.get("span_pts", 0) > 0 and eff.get("state") != "recalibrating":
                span_pts = eff["span_pts"]
                span_source = "learned"
            elif live_span:
                span_pts = live_span
                span_source = "live"
            else:
                span_pts = None
                span_source = "live"  # ignored by dose_zone when span is unusable
            target = (
                zone_cfg.refill_target_pct if zone_cfg.refill_target_pct is not None
                else tun.field_capacity_pct
            )
            # A triggered zone has dominant < floor; refilling only UP TO a target
            # below that floor would compute deficit <= 0 and dose 0 minutes while
            # still being reported as watered. Clamp the target up to the floor so a
            # triggered zone always has positive deficit (no-op whenever target >=
            # floor, which the default field_capacity_pct=87 always is here).
            target = max(target, targets[k].floor)
            depth_mm = (
                api_depths_for_dose.get(zone_cfg.rachio_zone_id)
                or zone_cfg.refill_depth_mm
            )
            dose = dosing.dose_zone(
                dominant_now=dominant_by_zone[k],
                refill_target=target,
                span_pts=span_pts,
                span_source=span_source,
                full_refill_min=plan.cycles_minutes(base, 1.0),
                refill_depth_mm=float(depth_mm),
                runtime_scale=targets[k].runtime_scale,
            )
            minutes[k] = dose.minutes
            doses[k] = dose
            dosing_sources[k] = dose.source
            # Active probe: while calibrating/recalibrating with headroom (and not
            # pinned), override the computed dose with a small growing probe so the
            # zone converges in days. cap_for_saturation prevents an overshoot into
            # the clipped region, and the probe is bounded to one full refill (min
            # above).
            pinned = zone_cfg.refill_span_pts > 0
            rec = eff or {}
            if tun.self_calibration_enabled and calibration.should_probe(
                    rec.get("state", "calibrating"), dominant_by_zone[k], pinned, tun):
                full_refill_min = plan.cycles_minutes(base, 1.0)
                pm = calibration.probe_minutes(
                    full_refill_min, rec.get("prior_minutes"), rec.get("last_rise"), tun)
                pm = calibration.cap_for_saturation(
                    pm, dominant_by_zone[k], rec.get("efficacy"), tun)
                pm = min(pm, full_refill_min)
                minutes[k] = pm
                dosing_sources[k] = "probe"
                # Reflect the probe in doses[k] so telemetry and _rain_skip_check see
                # the probe's actual (small) delivered depth, not the deficit-based
                # dose that dose_zone computed and we just overrode.
                probe_frac = min(pm / full_refill_min, 1.0) if full_refill_min > 0 else 0.0
                doses[k] = dosing.DoseResult(
                    minutes=pm,
                    frac=probe_frac,
                    effective_depth_mm=probe_frac * float(depth_mm),
                    deficit_pts=target - dominant_by_zone[k],
                    span_pts=span_pts or 0.0,
                    source="probe",
                )
        geo = {k: cfg.zones[k].geography for k in priority}
        adjacency = {k: cfg.zones[k].adjacency for k in priority}

        # The window cap describes the hours the grass will be WET, so it is sized
        # from the overnight forecast, not from conditions at plan time. Rain abort
        # during a run stays instantaneous (see _is_rain).
        wx = self._read_weather(tun)
        forecast_wx = self._read_forecast_weather()
        if forecast_wx is None:
            cap_source = "instant"
            _LOGGER.warning(
                "irrigation: overnight forecast unavailable; sizing the window from "
                "current conditions"
            )
            cap = weather.window_cap_minutes(wx, tun)
        else:
            cap_source = "forecast"
            cap = weather.window_cap_minutes(forecast_wx, tun)
        # The end anchor is per drought profile: Levels 0-2 finish at sunrise so
        # watering ends just as drying begins; Level 3 finishes at dawn, before any
        # sun, to minimise evaporative loss when water is scarce.
        end_offset = config.resolved_end_offset(profile, tun.end_offset_minutes)
        end = self._end_anchor_time(profile.end_anchor) - dt.timedelta(minutes=end_offset)

        # Clamp the disease-window cap to the time actually left before the end
        # anchor, so a plan can never be scheduled to start in the past or run past
        # dawn. Matters most for a mid-window restart (self-heal), where only part of
        # the window remains; it also keeps the nightly plan honest when the trigger
        # fires later than the theoretical window open.
        remaining = (end - self.port.now().astimezone(end.tzinfo)).total_seconds() / 60.0
        if remaining < cap:
            cap = max(0.0, remaining)

        the_plan = plan.build_plan(priority, minutes, geo, adjacency, cap, tun)
        for z in the_plan.dropped:
            uncompleted[z] = "insufficient window"

        start = end - dt.timedelta(minutes=the_plan.span_minutes)
        earliest_start = end - dt.timedelta(minutes=cap)
        # Calibrating zones skipped tonight for a bad sensor — the pre-dawn wait
        # re-checks these and folds in a probe if the sensor recovers.
        recovery_candidates = []
        if tun.self_calibration_enabled:
            for _k in cfg.zones:
                _rec = efficacy_store.get(_k) or {}
                if evaluate.recovery_candidate(
                        _rec.get("state", "calibrating"),
                        uncompleted.get(_k), SENSOR_SKIP_REASONS):
                    recovery_candidates.append(_k)
        return {
            "cfg": cfg, "tun": tun, "level": level, "priority": priority,
            "minutes": minutes, "uncompleted": uncompleted, "the_plan": the_plan,
            "start": start, "end": end,
            "wx": wx, "cap_minutes": cap, "earliest_start": earliest_start,
            "runtime_sources": runtime_sources,
            "forecast_wx": forecast_wx, "cap_source": cap_source,
            "horizon_hours": profile.rain_skip_horizon_hours,
            "end_anchor": profile.end_anchor,
            "end_offset_minutes": end_offset,
            "doses": doses,
            "dosing_sources": dosing_sources,
            "dominant_by_zone": dominant_by_zone,
            # Per-zone drought floor, for the window-start moisture re-check (which
            # runs after the wait, without re-deriving the drought profile).
            "floors": {k: targets[k].floor for k in targets},
            "api_runtimes": api_runtimes,
            "api_depths": api_depths_for_dose,
            "recovery_candidates": recovery_candidates,
        }

    def _pressure_pair(self, ctx):
        """(forecast-based, instantaneous) disease-pressure breakdowns.

        Report BOTH models: the forecast one drives the cap, the instantaneous one
        is the old behaviour. Side by side they are the calibration instrument for
        the retuned overnight thresholds — which is why the nightly RUN publishes
        them too, not just the preview. Comparing them required firing a preview by
        hand at 23:00 every night, which nobody was going to do.
        """
        pb_instant = weather.pressure_breakdown(ctx["wx"], ctx["tun"])
        if ctx["forecast_wx"] is None:
            return pb_instant, pb_instant
        return weather.pressure_breakdown(ctx["forecast_wx"], ctx["tun"]), pb_instant

    def _rain_skip_check(self, ctx):
        """(should_skip, detail) for the plan in `ctx`.

        The amount threshold is a fraction of the deficit-proportional EFFECTIVE
        delivered depth, averaged across the zones actually planned tonight — it
        answers "would this rain substitute for tonight's run?" rather than
        favouring the smallest or largest zone. With no zones planned there is
        nothing to skip, so it returns False.

        Fails open: any unavailable forecast value yields False (see
        weather.is_rain_skip).
        """
        planned = ctx["the_plan"].watered
        if not planned:
            return False, {}

        doses = ctx["doses"]
        depths = []
        for key in planned:
            eff = doses[key].effective_depth_mm
            if eff > 0:
                depths.append(eff)
        if not depths:
            return False, {}
        mean_depth = sum(depths) / len(depths)

        horizon = ctx["horizon_hours"]
        prob, amount = self._read_forecast_precip(horizon)
        skip = weather.is_rain_skip(prob, amount, mean_depth, ctx["tun"])
        detail = {
            "horizon_hours": horizon,
            "probability_pct": prob,
            "amount_mm": amount,
            "mean_effective_depth_mm": round(mean_depth, 2),
            "threshold_mm": round(ctx["tun"].rain_skip_refill_fraction * mean_depth, 2),
        }
        return skip, detail
