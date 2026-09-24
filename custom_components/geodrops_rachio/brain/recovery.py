"""Mid-run drop recovery decisions. Pure Python.

The pause-collapse model (see program.py) runs a whole night as one Rachio
schedule kept alive by device pauses. Rachio drops that schedule once its
cumulative pause passes a limit (~60 min observed 2026-08-27), and the in-block
watch then reports the next un-started water step as "never-started". This
module decides what to do with such a report: keep aborting (a genuine non-start
or a real stop), re-issue a fresh schedule for the water still owed (a drop we
can recover from), or give up (recovery exhausted, or a fresh schedule that will
not even start — Rachio is refusing water now).
"""
from __future__ import annotations

CONTINUE_ABORT = "continue-abort"
RECOVER = "recover"
GIVE_UP = "give-up"


def verdict(aborted_reason, delivered_since_issue, is_recovery_schedule,
            retries_used, max_retries):
    """Decide the response to a segment abort.

    Returns CONTINUE_ABORT | RECOVER | GIVE_UP.

    `delivered_since_issue` is minutes watered under the CURRENT schedule (reset
    to 0 on each re-issue) — the signal that separates "Rachio dropped a working
    schedule" (some water, then gone) from "Rachio will not water" (nothing,
    ever). `is_recovery_schedule` is False for the first schedule of a run and
    True after any re-issue. `retries_used` counts re-issues already spent;
    recovery is allowed while it stays below `max_retries` (<= 0 disables it).
    """
    if aborted_reason != "never-started":
        # Rain, standby, manual, external-stop: real stops. Never recover.
        return CONTINUE_ABORT
    if delivered_since_issue > 0:
        # Delivered, then dropped: the budget cap. Re-issue unless spent.
        return RECOVER if retries_used < max_retries else GIVE_UP
    # never-started with nothing delivered under this schedule:
    if is_recovery_schedule:
        # A fresh schedule that will not even start = Rachio refusing now.
        return GIVE_UP
    # The initial schedule never watered at all: the genuine never-started.
    return CONTINUE_ABORT


def remaining_after(steps, stopped_index):
    """Program Steps still owed after a drop at `stopped_index`.

    The water step that failed delivered nothing (never-started credits zero), so
    it is re-included in full: the remainder is the tail from that step onward.
    A drop is always detected ON a water step, so the tail never begins with a
    pause and is a valid standalone program for a fresh schedule.
    """
    return steps[stopped_index:]


# ─── waiting-run restart recovery ────────────────────────────────────────────

IGNORE = "ignore"
RE_ARM = "re-arm"
MISSED = "missed"


def startup_action(marker, now_iso):
    """Decide what _on_startup does about a planned-but-waiting run.

    The nightly plans at 23:00 then sleeps until the pre-dawn window. A restart
    during that sleep loses the in-memory run; only a persisted marker survives.
    The adapter writes the marker just before the sleep and clears it the instant
    the wait ends, so a live marker means "a run was waiting and had not yet
    opened a valve".

    Returns IGNORE | RE_ARM | MISSED.

    `marker` is the parsed marker dict (or None if absent). `window_end` is the
    ISO time the watering window closes (dawn - end_offset). A marker missing or
    carrying an unparseable `window_end` is treated as IGNORE - a malformed
    marker must never crash startup, and the safety-stop path still runs.
    """
    if not marker:
        return IGNORE
    import datetime
    try:
        window_end = datetime.datetime.fromisoformat(marker["window_end"])
        now = datetime.datetime.fromisoformat(now_iso)
    except (KeyError, TypeError, ValueError):
        return IGNORE
    # window_end comes from the tz-aware sun sensor; now may be naive local time.
    # Compare both as aware local times (a naive value is taken as local) —
    # comparing naive with aware raises TypeError and lost the night (<= v0.9.15).
    return RE_ARM if now.astimezone() < window_end.astimezone() else MISSED


# ─── interrupted-night resume ────────────────────────────────────────────────

RESUME = "resume"
INTERRUPTED = "interrupted"
# Below this, what is still owed is rounding, not water worth a schedule.
MIN_OWED_MINUTES = 1.0
# A progress doc whose window closed longer ago than this belongs to an earlier
# night (startup only checks overnight, so a daytime restart leaves it behind).
STALE_AFTER_HOURS = 12


def owed_minutes(planned, delivered, min_minutes=MIN_OWED_MINUTES):
    """{zone: minutes} still owed: planned minus delivered, crumbs dropped."""
    owed = {}
    for zone, minutes in planned.items():
        left = minutes - (delivered or {}).get(zone, 0)
        if left >= min_minutes:
            owed[zone] = left
    return owed


def resume_action(progress, now_iso):
    """Decide what startup does about a night's persisted run progress.

    Returns (IGNORE | RESUME | INTERRUPTED, owed). The run writes its plan and
    what it has delivered, counting each water step as delivered the moment it
    starts, so an interruption at worst leaves one step short (never doubled).

    - IGNORE: no progress, a malformed doc, or one from an earlier night.
    - RESUME: still inside the watering window with water owed — water only
      that, never re-planning from moisture readings that lag the watering.
    - INTERRUPTED: the window has closed, or nothing is owed: record the night,
      water nothing.
    """
    if not isinstance(progress, dict):
        return IGNORE, {}
    import datetime
    planned = progress.get("planned")
    delivered = progress.get("delivered") or {}
    if not isinstance(planned, dict) or not isinstance(delivered, dict):
        return IGNORE, {}
    try:
        window_end = datetime.datetime.fromisoformat(progress["window_end"]).astimezone()
        now = datetime.datetime.fromisoformat(now_iso).astimezone()
    except (KeyError, TypeError, ValueError):
        return IGNORE, {}
    if now - window_end > datetime.timedelta(hours=STALE_AFTER_HOURS):
        return IGNORE, {}
    owed = owed_minutes(planned, delivered)
    if now < window_end and owed:
        return RESUME, owed
    return INTERRUPTED, owed
