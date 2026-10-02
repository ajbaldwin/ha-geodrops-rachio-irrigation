"""Per-zone watering evaluation and priority ordering. Pure Python."""
from __future__ import annotations

import random
from collections.abc import Container
from dataclasses import dataclass

from .drought import EffectiveTarget
from .sensors import ZoneReading


@dataclass(frozen=True)
class ZoneEvaluation:
    key: str
    needs_water: bool
    index_deficit: int
    dominant_deficit: float


def evaluate_zone(reading: ZoneReading, target: EffectiveTarget) -> ZoneEvaluation:
    # Online implies a reading; the None checks only say so to the type checker.
    if (not reading.online or reading.dominant is None
            or reading.index_rank is None):
        return ZoneEvaluation(reading.key, False, 0, 0.0)
    needs = reading.dominant < target.floor
    return ZoneEvaluation(
        key=reading.key,
        needs_water=needs,
        index_deficit=target.rank - reading.index_rank,
        dominant_deficit=target.floor - reading.dominant,
    )


def revalidate_zone(online: bool, dominant_now: float | None, dosing_source: str | None,
                    floor: float, ceiling: float) -> str | None:
    """Re-decide, at window start, whether a PLANNED zone should still water.

    The nightly plan is built hours before the pre-dawn window; rain that lands
    in the gap can raise a zone's moisture without being visible at plan time
    (GeoDrops sensors report on a slow cadence). Given a FRESH reading, return a
    drop reason, or None to keep the zone:

    - sensor offline now -> None (fail-open; cannot prove the soil is wet, and
      the plan already qualified the sensor).
    - probe zone -> "saturated" if dominant_now >= ceiling
      (probe_headroom_ceiling); a probe into saturated soil is rejected anyway.
    - deficit zone -> "above-floor" if dominant_now >= floor; the trigger reason
      (dominant below floor) is gone.

    Boundary is inclusive (>=): a reading exactly at the line drops.
    """
    if not online or dominant_now is None:
        return None
    if dosing_source == "probe":
        if dominant_now >= ceiling:
            return "saturated"
        return None
    if dominant_now >= floor:
        return "above-floor"
    return None


def recovery_candidate(state: str, uncompleted_reason: str | None,
                       sensor_skip_reasons: Container[str | None]) -> bool:
    """True iff a zone is worth re-checking during the pre-dawn wait: it is
    calibrating/recalibrating AND was skipped from tonight's plan for a SENSOR
    reason (offline / low-quality), so a mid-window sensor recovery could still
    earn a probe. Excluded ("excluded") and above-floor (None) skips are not
    candidates.
    """
    if state not in ("calibrating", "recalibrating"):
        return False
    return uncompleted_reason in sensor_skip_reasons


def sort_by_priority(evals: list[ZoneEvaluation], rng: random.Random) -> list[ZoneEvaluation]:
    """Zones needing water, ordered by index deficit, then dominant-% deficit,
    then random.

    NOTE (pyscript): do NOT use `sorted(..., key=lambda e: ...)` here. pyscript
    lambdas (and nested defs) do NOT capture enclosing-function locals, so
    referencing `rng` inside the key raises
    `NameError: name 'rng' is not defined`. Comprehensions DO see enclosing
    locals, so the filter below is fine. Decorate in an explicit loop (which can
    see `rng`) with an all-numeric tuple, sort that, then map back by index — the
    trailing index also guarantees the sort never has to compare ZoneEvaluation
    objects, which are not orderable.
    """
    needing = [e for e in evals if e.needs_water]
    decorated: list[tuple[int, float, float, int]] = []
    for i in range(len(needing)):
        e = needing[i]
        decorated.append((-e.index_deficit, -e.dominant_deficit, rng.random(), i))
    result: list[ZoneEvaluation] = []
    for row in sorted(decorated):
        result.append(needing[row[3]])
    return result
