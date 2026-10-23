"""Calibration functions: efficacy↔span, probe sizing, saturation cap. Pure Python (no pyscript globals)."""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Tunables


# Reject reasons that mean "the probe was too small to measure": the next one grows.
TOO_SMALL = ("no_rise", "small_rise")
# Rachio runs whole minutes, so a full-refill probe can deliver up to this much less.
FULL_REFILL_TOLERANCE_MIN = 1.0


def efficacy_to_span(efficacy: float, full_refill_min: float) -> float:
    """Convert efficacy to span in points.

    Returns the points range that a full refill buys at the given efficacy.
    """
    return efficacy * full_refill_min


def probe_minutes(full_refill_min: float, prior_minutes: float | None,
                  last_rise: float | None, t: Tunables) -> float:
    """Calculate probe window size for next calibration cycle.

    `t` is a Tunables object with .probe_fraction, .probe_floor_minutes,
    .probe_growth, and .measurable_rise_pts attributes.

    First probe (prior_minutes is None): uses fraction of window or floor, whichever is larger.
    Subsequent probes: grows on small rise, holds steady on measurable rise.
    """
    if prior_minutes is None:
        candidate = t.probe_fraction * full_refill_min
        return max(candidate, t.probe_floor_minutes)
    elif last_rise is not None and last_rise < t.measurable_rise_pts:
        return prior_minutes * t.probe_growth
    else:
        return prior_minutes


def cap_for_saturation(minutes: float, dominant_now: float, efficacy: float | None,
                       t: Tunables) -> float:
    """Cap probe minutes to prevent saturation.

    If efficacy is known (not None and > 0), calculates the maximum watering
    that keeps soil moisture below the saturation rejection threshold.
    Otherwise returns minutes unchanged.

    `t` is a Tunables object with .saturation_reject attribute.
    """
    if efficacy and efficacy > 0:
        headroom = t.saturation_reject - dominant_now
        max_min = headroom / efficacy
        return max(0.0, min(minutes, max_min))
    else:
        return minutes


@dataclass(frozen=True)
class Observation:
    """A calibration observation: pre/post watering soil moisture and metadata."""
    zone: str
    pre_dominant: float
    minutes: float
    settled_dominant: float
    qcn_training: bool
    rained: bool
    sensor_ok: bool


def classify(obs: Observation, t: Tunables) -> str:
    """Classify observation quality; returns "ok" or a reject reason.

    Priority order (first match wins):
    1. not obs.sensor_ok → "unavailable"
    2. obs.qcn_training → "training"
    3. obs.rained → "rain"
    4. obs.settled_dominant >= t.saturation_reject → "saturated"
    5. (obs.settled_dominant - obs.pre_dominant) <= 0 → "no_rise"
    6. rise < t.measurable_rise_pts → "small_rise" (inside probe noise: the
       probe is too small to measure, and a rate learned from it could be off
       by its whole size)
    7. else → "ok"
    """
    if not obs.sensor_ok:
        return "unavailable"
    if obs.qcn_training:
        return "training"
    if obs.rained:
        return "rain"
    if obs.settled_dominant >= t.saturation_reject:
        return "saturated"
    rise = obs.settled_dominant - obs.pre_dominant
    if rise <= 0:
        return "no_rise"
    if rise < t.measurable_rise_pts:
        return "small_rise"
    return "ok"


def update_efficacy(prev: float | None, obs: Observation, t: Tunables) -> float:
    """Update efficacy with EWMA from a new observation.

    efficacy_obs = (settled - pre) / minutes
    If prev is None: return efficacy_obs (bootstrap)
    Else: blend with EWMA using t.calibration_ewma_alpha
    """
    efficacy_obs = (obs.settled_dominant - obs.pre_dominant) / obs.minutes
    if prev is None:
        return efficacy_obs
    return t.calibration_ewma_alpha * efficacy_obs + (1 - t.calibration_ewma_alpha) * prev


def converged(recent: Sequence[float], t: Tunables) -> bool:
    """Check if recent efficacy samples have converged.

    recent: list of efficacy values
    Returns True if:
    - length >= t.convergence_samples AND
    - mean != 0 AND
    - (max - min) <= convergence_tolerance * mean
    """
    if len(recent) < t.convergence_samples:
        return False

    # Compute mean explicitly (no generator expressions per pyscript)
    total = 0.0
    for val in recent:
        total = total + val
    mean = total / len(recent)

    if mean == 0:
        return False

    # Find max and min
    max_val = recent[0]
    min_val = recent[0]
    for i in range(len(recent)):
        if recent[i] > max_val:
            max_val = recent[i]
        if recent[i] < min_val:
            min_val = recent[i]

    tolerance_band = t.convergence_tolerance * mean
    return (max_val - min_val) <= tolerance_band


def next_state(state: str, converged_flag: bool, training: bool, miss_streak: int,
               t: Tunables) -> str:
    """State machine: compute next calibration state.

    Transitions:
    - if training: always transition to "recalibrating"
    - elif state in ("calibrating", "recalibrating") and converged_flag: -> "converged"
    - elif state == "converged" and miss_streak >= t.convergence_samples: -> "calibrating"
    - else: return state unchanged

    `t` is a Tunables object with .convergence_samples attribute.
    """
    if training:
        return "recalibrating"

    if state in ("calibrating", "recalibrating") and converged_flag:
        return "converged"

    if state == "converged" and miss_streak >= t.convergence_samples:
        return "calibrating"

    return state


def exclusion_return(rec: Mapping[str, Any], now: dt.datetime,
                     threshold_hours: float) -> dict[str, Any]:
    """Resolve a zone's return from exclusion, given its efficacy record.

    If the record carries an `excluded_since` stamp and the zone was excluded for
    at least `threshold_hours`, reset it to recalibrating (its learned efficacy is
    stale — the soil likely changed, e.g. overseed). A shorter (accidental)
    exclusion keeps the existing calibration. Either way the stamp is cleared. A
    record with no stamp is returned unchanged. `now` is a datetime whose tz-
    awareness matches how `excluded_since` was written.
    """
    out = dict(rec)
    stamp = out.pop("excluded_since", None)
    if stamp is None:
        return out
    try:
        since = dt.datetime.fromisoformat(stamp)
        hours = (now - since).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return out  # malformed stamp: clear it, keep the calibration
    if hours >= threshold_hours:
        out["state"] = "recalibrating"
        out["efficacy"] = None
        out["span_pts"] = 0
        out["recent"] = []
        out["miss_streak"] = 0
    return out


def apply_reject(zrec: Mapping[str, Any], reason: str, minutes: float, rise: float,
                 t: Tunables) -> dict[str, Any]:
    """Return a copy of zrec updated for a rejected calibration observation.

    Both dose-size rejects resize the probe so it self-corrects toward the clean
    measurable band:
    - "no_rise" / "small_rise": the probe was too small — record its size and
      (unmeasurable) rise so probe_minutes grows the next probe.
    - "saturated": the probe was too large — record the shrunk size (÷probe_shrink)
      with last_rise=None so probe_minutes holds there next time.
    Confounded reasons (rain/unavailable/training) never tested efficacy, so they
    leave the size untouched. Always stamps last_reject_reason.
    """
    out = dict(zrec)
    out["last_reject_reason"] = reason
    if reason in TOO_SMALL:
        out["prior_minutes"] = minutes
        out["last_rise"] = rise
    elif reason == "saturated":
        out["prior_minutes"] = minutes / t.probe_shrink
        out["last_rise"] = None
    return out


def unresponsive(reason: str, minutes: float, full_refill_min: float | None) -> bool:
    """Did a full refill still produce no measurable rise?

    A full refill is the most one night delivers, so a probe grown to it has
    nowhere left to go: no measurable rise from it means the water is not
    reaching the probe (a clogged or misaligned head, a probe outside the
    spray pattern, fast-draining soil, or a probe needing GeoDrops
    calibration). `minutes` within FULL_REFILL_TOLERANCE_MIN of the full
    refill counts, since Rachio runs whole minutes. An obs queued before its
    full refill was recorded (None or 0) never decides it.
    """
    if reason not in TOO_SMALL or not full_refill_min:
        return False
    return minutes >= full_refill_min - FULL_REFILL_TOLERANCE_MIN


def resume(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Restart an unresponsive zone's calibration from a first probe.

    For after its heads or probe were checked: the learned rate and the stuck
    probe size are dropped, the lifetime n_obs and retention are kept.
    """
    out = dict(rec)
    out.pop("unresponsive_since", None)
    out["state"] = "recalibrating"
    out["efficacy"] = None
    out["span_pts"] = 0
    out["recent"] = []
    out["prior_minutes"] = None
    out["last_rise"] = None
    out["miss_streak"] = 0
    out["last_reject_reason"] = None
    return out


def should_probe(state: str, dominant_now: float, pinned: bool, t: Tunables) -> bool:
    """Determine whether to probe (collect calibration observation) now.

    Returns True iff:
    - state in ("calibrating", "recalibrating") AND
    - not pinned AND
    - dominant_now < t.probe_headroom_ceiling

    `t` is a Tunables object with .probe_headroom_ceiling attribute.
    """
    if state not in ("calibrating", "recalibrating"):
        return False

    if pinned:
        return False

    if dominant_now >= t.probe_headroom_ceiling:
        return False

    return True


def settle_decision(now: dt.datetime, run_end: dt.datetime, retain_hours: float,
                    max_wait_hours: float, has_sample: bool,
                    has_retained: bool = False) -> str:
    """Decide how to handle one pending calibration obs at this poll.

    Accumulate model: the poll folds each genuine reading into the obs (peak +
    retained) until finalize. All datetimes tz-aware.

    Returns:
      "accumulate" - before finalize (now < run_end + retain_hours), OR past it
                     without a retained sample yet but still within the grace
                     window. Keep the obs pending and keep collecting.
      "finalize"   - now >= run_end + retain_hours AND a retained sample was
                     captured (has_retained); or past finalize + max_wait_hours
                     with only a peak sample (has_sample), so efficacy still
                     learns and retention is left unchanged. Compute + accept.
      "expired"    - past finalize + max_wait_hours with no sample ever. Drop
                     the obs (never reject, never touch the model).

    The retained sample is gated on when its reading was TAKEN, and a GeoDrops
    reading reaches HA hours after that, so finalize waits through the grace
    window for one rather than settling for peak-only at the first ripe poll.
    A captured sample always finalizes, at the latest once the grace window ends.
    """
    finalize_at = run_end + dt.timedelta(hours=retain_hours)
    if now < finalize_at:
        return "accumulate"
    if has_retained:
        return "finalize"
    if now < finalize_at + dt.timedelta(hours=max_wait_hours):
        return "accumulate"
    return "finalize" if has_sample else "expired"


def accumulate_sample(
        peak: float | None, retained: float | None, last_seen: dt.datetime | None,
        value: float | None, read_at: dt.datetime | None,
        run_end: dt.datetime, settle_hours: float,
) -> tuple[float | None, float | None, dt.datetime | None, bool]:
    """Fold one candidate sensor reading into an obs accumulator.

    `read_at` is when the reading was TAKEN (GeoDrops' Last Reading), not when
    HA received it. Counts only a genuinely new reading (read_at strictly newer
    than last_seen — a stale republish, or GeoDrops' reprocess rewriting the
    value, carries the same read_at) taken at or after run_end (an earlier
    reading predates the watering's end, however late it was uploaded). peak is
    the running max from run_end on; retained is the latest reading taken at or
    after run_end + settle_hours. Returns (peak, retained, last_seen, changed).
    """
    if value is None or read_at is None:
        return peak, retained, last_seen, False
    if last_seen is not None and read_at <= last_seen:
        return peak, retained, last_seen, False
    if read_at < run_end:
        return peak, retained, last_seen, False
    new_peak = value if peak is None else max(peak, value)
    new_ret = retained
    if read_at >= run_end + dt.timedelta(hours=settle_hours):
        new_ret = value
    return new_peak, new_ret, read_at, True


def upgrade_pre(
        pre: float | None, pre_read_at: dt.datetime | None,
        value: float | None, read_at: dt.datetime | None,
        zone_start: dt.datetime | None,
) -> tuple[float | None, dt.datetime | None]:
    """Move the pre-watering baseline to a later reading taken before the valve opened.

    The baseline snapshot is whatever HA held when watering began, and a
    GeoDrops reading reaches HA up to an hour after it is TAKEN, so a reading
    taken closer to the zone's valve-open can land after the run. It replaces
    the baseline when it was taken strictly before `zone_start` and strictly
    after `pre_read_at` (a snapshot with no reading time gives way to any).
    Readings taken once the valve opened describe the watering, not the soil
    before it. No `zone_start` (an obs queued before valve-open times were
    recorded) keeps the baseline. Returns (pre, pre_read_at).
    """
    if value is None or read_at is None or zone_start is None:
        return pre, pre_read_at
    if read_at >= zone_start:
        return pre, pre_read_at
    if pre_read_at is not None and read_at <= pre_read_at:
        return pre, pre_read_at
    return value, read_at


def retention_factor(pre: float, peak: float, retained: float | None,
                     floor: float) -> float | None:
    """r = (retained - pre) / (peak - pre), clamped to [floor, 1.0].

    None when it cannot be computed: no retained sample, or peak did not rise
    above pre (peak_rise <= 0).
    """
    if retained is None:
        return None
    peak_rise = peak - pre
    if peak_rise <= 0:
        return None
    r = (retained - pre) / peak_rise
    return max(floor, min(1.0, r))


def retained_span(efficacy: float, retention: float | None, base: float,
                  span_min: float, span_max: float) -> float:
    """Dominant points a full refill RETAINS = efficacy(peak) * r * base, clamped.

    retention None (pre-retention) is treated as 1.0 (peak span).
    """
    r = 1.0 if retention is None else retention
    span = efficacy * r * base
    return max(span_min, min(span_max, span))


def ewma(prev: float | None, value: float, alpha: float) -> float:
    """Exponentially-weighted blend; bootstraps to value when prev is None."""
    if prev is None:
        return value
    return alpha * value + (1 - alpha) * prev
