"""Learning passes: the 06:00 forecast calibration and the 30-min settle poll.

Ported from bundled_app/geodrops_rachio.py lines 2372-2459 and 2467-2601.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..brain import calibration, sensors, weather
from .base import STATUS_ENTITY
from .store import PENDING_OBS

_LOGGER = logging.getLogger(__name__)


class LearningMixin:
    async def irrigation_calibrate(self):
        """Score last night's forecast against what actually happened.

        Runs at 06:00 for a reason: `sensor.observed_overnight_*` are rolling
        6-hour means, so at 06:00 they cover 00:00-06:00 — exactly the window
        `sensor.forecast_overnight_*` averages (`dt.hour < 6`). Same window, same
        statistic, so the two are actually comparable. Reading them at any other
        hour silently compares different periods.

        The forecast side is NOT recomputed here: by 06:00 the forecast sensors
        describe the hours before TOMORROW's dawn. It is read back from the record
        the nightly run left behind.

        Publishes the comparison and logs a one-line verdict, so a week of Logbook
        entries answers the threshold question on its own. Nothing is auto-tuned —
        these thresholds encode turf pathology, not a fitted parameter, and a loop
        quietly adjusting them would change watering with nothing to catch it.
        """
        if self._run_in_progress:
            _LOGGER.warning(
                "irrigation: calibration skipped — an irrigation run is in "
                "progress and owns the shared config globals"
            )
            return
        cfg = self._load_cfg()
        # Runs on its own cron, outside _plan_and_run, so it must load these
        # globals itself before _read_observed_overnight() can use them.
        self._current_cfg = cfg
        self._current_bindings = cfg.bindings
        tun = cfg.tunables
        observed_wx = self._read_observed_overnight()
        if observed_wx is None:
            _LOGGER.warning(
                "irrigation: overnight observed means unavailable; skipping "
                "calibration for last night"
            )
            return
        attrs = (self.records.get("last_nightly") or {}).get("attributes")
        forecast_pb = attrs.get("pressure_forecast") if attrs is not None else None
        if not forecast_pb:
            await self._activity("Calibration skipped — no nightly run to compare against")
            return

        observed_pb = weather.pressure_breakdown(observed_wx, tun)
        agreement = weather.pressure_agreement(forecast_pb, observed_pb)
        stamp = self._naive_now().isoformat(timespec="seconds")
        await self._publish_record(
            "calibration",
            len(agreement["mismatches"]),
            {
                "friendly_name": "Irrigation Forecast Calibration",
                "updated": stamp,
                "window": "00:00-06:00",
                "pressure_forecast": forecast_pb,
                "pressure_observed": observed_pb,
                "observed_temp_f": observed_wx.temp_f,
                "observed_rh_pct": observed_wx.rh_pct,
                "observed_wind_mph": observed_wx.wind_mph,
                "signals": agreement["signals"],
                "mismatches": agreement["mismatches"],
                "thresholds": {
                    "warm_temp_f": tun.warm_temp_f,
                    "humid_rh_pct": tun.humid_rh_pct,
                    "stagnant_wind_mph": tun.stagnant_wind_mph,
                },
            },
        )
        if agreement["mismatches"]:
            detail = ", ".join([
                f"{name} {agreement['signals'][name]}"
                for name in agreement["mismatches"]
            ])
        else:
            detail = "all signals agree"
        await self._activity(
            f"Calibration: forecast {agreement['forecast_count']}/3 vs observed "
            f"{agreement['observed_count']}/3 — {detail} "
            f"(RH {observed_wx.rh_pct}, wind {observed_wx.wind_mph}, "
            f"temp {observed_wx.temp_f})",
            entity_id=STATUS_ENTITY,
        )

    async def _settle_and_learn(self):
        """Read settled dominant for runs whose settle window has elapsed, reject
        confounded observations, and update per-zone efficacy (feeds the learned span)."""
        try:
            cfg = self._load_cfg()
        except Exception as err:
            _LOGGER.warning(f"irrigation: settle-and-learn skipped; config load failed ({err})")
            return
        tun = cfg.tunables
        if not tun.self_calibration_enabled:
            return
        pending = self.store.read(PENDING_OBS)
        if not isinstance(pending, list) or not pending:
            return
        store = self._read_efficacy_store()
        now = self.port.now()
        api_runtimes = None      # lazy: only fetched once an obs is ready to measure
        rained = None            # lazy: same
        remaining = []
        learned = 0
        dropped = 0
        for rec in pending:
            try:
                run_end = dt.datetime.fromisoformat(rec["run_end_iso"])
            except (KeyError, ValueError):
                continue
            zone = rec.get("zone")
            zone_cfg = cfg.zones.get(zone)
            if zone_cfg is None:
                continue  # obs for a zone no longer configured: drop it
            try:
                # --- accumulate this poll's reading into the obs (freshness-gated) ---
                signals = self._read_zone_signals(zone_cfg)
                reading = sensors.read_zone(zone_cfg, signals)
                value = reading.dominant if reading.online else None
                last_updated = self._sensor_last_updated(zone_cfg.dominant_sensor)
                try:
                    last_seen = (dt.datetime.fromisoformat(rec["last_seen_updated"])
                                 if rec.get("last_seen_updated") else None)
                except ValueError:
                    last_seen = None
                peak, retained, last_seen, _ch = calibration.accumulate_sample(
                    rec.get("peak"), rec.get("retained"), last_seen,
                    value, last_updated, now, run_end, tun.settle_hours)
                rec["peak"] = peak
                rec["retained"] = retained
                rec["last_seen_updated"] = last_seen.isoformat() if last_seen else None
                # --- decide ---
                decision = calibration.settle_decision(
                    now, run_end, tun.retain_hours, tun.settle_max_wait_hours,
                    peak is not None)
            except Exception as err:
                # A single zone's missing/renamed sensor (or any other accumulate
                # failure) must not abort the whole poll and must not drop the
                # observation — keep it for the next poll to retry.
                _LOGGER.warning(f"irrigation: settle-and-learn skipped a record ({err})")
                remaining.append(rec)
                continue
            if decision == "accumulate":
                remaining.append(rec)
                continue
            if decision == "expired":
                dropped += 1
                continue  # inconclusive: drop, never reject, no model change
            # decision == "finalize"
            if api_runtimes is None:
                api_runtimes = await self.get_runtimes()
                rained = self._rained_since_run(cfg.bindings, tun)
            try:
                pre = rec.get("pre_dominant")
                minutes = rec.get("minutes")
                if pre is None or not minutes:
                    continue
                quals = [(q or "").strip() for q in signals.qualities]
                qcn_training = (len(quals) == 3 and quals[0] == "Training"
                                and quals[1] == "Training" and quals[2] == "Training")
                # settled_dominant = PEAK: classify's no_rise (rise<=0) and saturated
                # (>=95) both key off the max the soil reached.
                obs = calibration.Observation(
                    zone=zone, pre_dominant=pre, minutes=minutes,
                    settled_dominant=peak, qcn_training=qcn_training,
                    rained=rained, sensor_ok=True,
                )
                reason = calibration.classify(obs, tun)
                zrec = store.get(zone) or {}
                if reason == "ok":
                    prev = zrec.get("efficacy")
                    eff = calibration.update_efficacy(prev, obs, tun)   # peak rate
                    eff_obs = (peak - pre) / minutes
                    recent = (zrec.get("recent") or []) + [eff_obs]
                    if len(recent) > tun.convergence_samples:
                        recent = recent[-tun.convergence_samples:]
                    r_obs = calibration.retention_factor(pre, peak, retained, tun.retention_floor)
                    prev_r = zrec.get("retention")
                    r = prev_r if r_obs is None else calibration.ewma(prev_r, r_obs, tun.calibration_ewma_alpha)
                    base = api_runtimes.get(zone_cfg.rachio_zone_id) or zone_cfg.runtime_minutes
                    span = calibration.retained_span(eff, r, base, tun.span_min, tun.span_max)
                    miss = zrec.get("miss_streak") or 0
                    if prev and prev > 0 and abs(eff_obs - prev) / prev > tun.convergence_tolerance:
                        miss = miss + 1
                    else:
                        miss = 0
                    conv = calibration.converged(recent, tun)
                    state_name = calibration.next_state(
                        zrec.get("state", "calibrating"), conv, False, miss, tun)
                    zrec = {
                        "state": state_name, "efficacy": eff, "retention": r,
                        "span_pts": span, "recent": recent,
                        "n_obs": (zrec.get("n_obs") or 0) + 1,
                        "prior_minutes": minutes, "last_rise": (peak - pre),
                        "miss_streak": miss,
                        "last_updated": now.isoformat(), "last_reject_reason": None,
                    }
                    learned += 1
                elif reason == "training":
                    zrec["state"] = calibration.next_state(
                        zrec.get("state", "calibrating"), False, True, 0, tun)
                    zrec["efficacy"] = None
                    zrec["span_pts"] = 0
                    zrec["recent"] = []
                    zrec["miss_streak"] = 0
                    zrec["last_reject_reason"] = reason
                else:
                    zrec = calibration.apply_reject(zrec, reason, minutes, peak - pre, tun)
                store[zone] = zrec
            except Exception as err:
                _LOGGER.warning(f"irrigation: settle-and-learn skipped a record ({err})")
                continue
        await self.store.write(PENDING_OBS, remaining)
        await self._write_efficacy_store(store)
        if learned or dropped:
            _LOGGER.info(
                f"irrigation: settle-and-learn updated {learned} zone(s), "
                f"dropped {dropped} inconclusive")
