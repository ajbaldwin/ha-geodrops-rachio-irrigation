"""Notification and calendar text builders. Pure Python."""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import Tunables

# Never zero: HA's Local Calendar enforces a minimum event duration of one
# second, so a point-in-time entry is rejected outright.
_MARKER = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class RunResult:
    watered: list[str]
    uncompleted: dict[str, str]  # zone -> reason
    start: str  # when watering actually began (== end when nothing watered)
    end: str    # when the run finished
    per_zone_minutes: dict[str, float]
    standby: bool
    # The disease window the run was allowed to use: a 4-6 h span ending just
    # before dawn, independent of how many zones watered. Kept separate from
    # start/end so a no-water night reports the real window instead of a
    # zero-length one. Defaulted so existing constructions stay valid.
    window_start: str = ""
    window_end: str = ""
    window_hours: float = 0.0
    # Full, tz-aware ISO instant when watering actually finished (valve close),
    # e.g. "2026-09-16T04:55:03-04:00". The time-only `end` above is for human
    # display; end_iso is what a TIMESTAMP consumer (the wrapper's Last Watered
    # sensor) parses. Empty on nights nothing watered (standby / rain-skip).
    end_iso: str = ""
    # How many times the collapsed Rachio schedule was dropped mid-run and
    # re-issued. 0 on a healthy night; defaulted so existing constructions stay
    # valid.
    recoveries: int = 0
    # {zone: ISO instant that zone's valve last closed}. A zone that waters
    # early in a long run finished well before end_iso; per-zone Last watered
    # reads this. Empty when nothing watered.
    zone_end_iso: dict[str, str] = field(default_factory=dict)
    # One line on what frost did to the night ("" when nothing), e.g. the
    # finish moving later or a skip; see frost_note / frost_live_note.
    note: str = ""


def _hhmm(iso: str | None) -> str:
    """"08:00" for an ISO instant, in local time; "" when there is none."""
    if not iso:
        return ""
    return dt.datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def frost_note(frost: Mapping[str, Any] | None, tun: Tunables) -> str:
    """The recap line for a plan's frost record: the finish moved later, or
    the night skipped; "" when frost changed nothing."""
    if not frost:
        return ""
    clear = _hhmm(frost.get("clear_at"))
    below = f"below {tun.frost_temp_f:g}°F"
    if frost.get("skip"):
        if clear:
            return (f"Frost: forecast {below} until {clear}, past the latest "
                    f"finish (sunrise + {tun.frost_max_delay_minutes} min)")
        return f"Frost: forecast {below}"
    shift = frost.get("shift_minutes") or 0
    if shift <= 0:
        return ""
    until = f" until {clear}" if clear else ""
    return f"Frost: finish moved {shift} min later (forecast {below}{until})"


def frost_live_note(temp_f: float, tun: Tunables) -> str:
    """The recap line for a night the live frost check skipped."""
    return (f"Frost: {temp_f:.1f}°F at window start "
            f"(below {tun.frost_skip_temp_f:g}°F)")


def display_name(zone_key: str) -> str:
    """Operator-facing name for a zone: "zone_a" -> "Zone A".

    Zone keys are config identifiers; every human-readable surface (phone
    notification, calendar entry, Logbook) shows this instead. Title-casing the
    underscored key reproduces the zone names configured in Rachio for all
    managed zones, so no separate mapping has to be kept in sync.

    Raw keys are deliberately retained in the `pyscript.irrigation_*` state
    attributes, where they are diagnostic and must match `config.yaml`.
    """
    return zone_key.replace("_", " ").title()


# Skip reasons whose internal key is not fit to show. The keys stay as they are
# in the plan/run records and state attributes, which code and tests compare.
_REASON_TEXT = {"probe_inactive": "Inactive"}


def display_reason(reason: str) -> str:
    """Operator-facing text for a zone's skip reason: "probe_inactive" ->
    "Inactive"; a reason already written as words is shown as given."""
    return _REASON_TEXT.get(reason, reason)


def _window_line(r: RunResult) -> str:
    """"Window: 00:52–04:52 (4.0h)", or "" when the window is unknown."""
    if not (r.window_start and r.window_end):
        return ""
    return f"Window: {r.window_start}–{r.window_end} ({r.window_hours}h)"


def format_notification(r: RunResult) -> str:
    if r.standby:
        return "Irrigation Standby — system paused, no zones watered."
    lines = []
    if r.watered:
        names = [display_name(z) for z in r.watered]
        lines.append(f"Watered {len(r.watered)}: {', '.join(names)}")
        lines.append(f"Watered {r.start}–{r.end}")
    else:
        lines.append("No zones watered.")
    window = _window_line(r)
    if window:
        lines.append(window)
    if r.note:
        lines.append(r.note)
    # An inactive probe is out of service until the user turns it back on, so
    # repeating it in every night's push is noise. The calendar entry and the
    # zone's Logbook still record it.
    parts = [f"{display_name(z)} ({display_reason(reason)})"
             for z, reason in r.uncompleted.items() if reason != "probe_inactive"]
    if parts:
        lines.append(f"Not completed: {', '.join(parts)}")
    if r.recoveries:
        plural = "s" if r.recoveries != 1 else ""
        lines.append(f"Rachio dropped the schedule {r.recoveries} time{plural}.")
    return "\n".join(lines)


def format_preview(
    drought_level: str,
    planned: list[tuple[str, float]],
    skipped: Mapping[str, str],
    window_note: str = "",
    watering_note: str = "",
) -> str:
    """Human-readable dry-run summary of the plan that WOULD run (no watering).

    `planned` is an ordered list of (zone, minutes) that would water; `skipped`
    maps zone -> reason (offline / insufficient window).

    `window_note` describes the DISEASE WINDOW — a fixed 4-6 h span ending just
    before dawn, independent of how many zones water. `watering_note` describes
    the watering packed WITHIN that window (total span + when it starts). Keeping
    them separate avoids the old bug where an empty plan showed a 0-minute
    "window". The leading line omits an "Irrigation preview" prefix — the
    notification title already carries it.
    """
    # Plan first (the actionable part), then skipped, then the window + drought
    # context last — the watering plan reads at the top of the message.
    lines = []
    if planned:
        lines.append(f"Would water {len(planned)}:")
        for zone, minutes in planned:
            lines.append(f"- {display_name(zone)}: {int(minutes)} min")
        if watering_note:
            lines.append(watering_note)
    else:
        lines.append("Would water nothing (no zones below target, or none fit).")
    if skipped:
        lines.append("Skipped:")
        for zone, reason in skipped.items():
            lines.append(f"- {display_name(zone)}: {display_reason(reason)}")
    if window_note:
        lines.append(window_note)
    lines.append(f"Drought: {drought_level}")
    return "\n".join(lines)


def format_calendar(r: RunResult, tun_cycle: int, tun_soak: int) -> tuple[str, str]:
    if r.standby:
        return ("Irrigation Standby", "System in standby — no watering performed.")
    total = sum(r.per_zone_minutes.values())
    title = f"Irrigation — {len(r.watered)} zones, {int(total)} min"
    lines = []
    window = _window_line(r)
    if window:
        lines.append(window)
    if r.watered:
        # Actual watering times — only meaningful when something ran. With
        # nothing watered these collapse to a zero-length "window".
        lines.append(f"Watered: {r.start}–{r.end}")
    if r.note:
        lines.append(r.note)
    lines.append(f"Cycle/soak: {tun_cycle}/{tun_soak} min")
    for z in r.watered:
        lines.append(f"- {display_name(z)}: {int(r.per_zone_minutes.get(z, 0))} min")
    if r.uncompleted:
        lines.append("Not completed:")
        for z, reason in r.uncompleted.items():
            lines.append(f"- {display_name(z)}: {display_reason(reason)}")
    return (title, "\n".join(lines))


def calendar_span(started_at: dt.datetime | None, ended_at: dt.datetime | None,
                  fallback: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """(start, end) for the recap's calendar entry, in ONE timezone.

    Both ends MUST carry the same UTC offset. HA's calendar.create_event
    validates that and rejects the pair otherwise ("Expected all values to have
    the same timezone") — and the two ends arrive from sources that disagree:
    the run's start descends from `sensor.sun_next_dawn`, which HA serves in
    UTC, while the finish time is local. Python compares aware datetimes across
    zones without complaint, so nothing catches the mismatch until the service
    call fails.

    A real watering interval spans start to finish. Anything else — nothing
    watered, or an end that does not follow its start — becomes a one-minute
    marker at `fallback`, because there is no interval to draw and a
    zero-length event would be rejected.
    """
    if started_at is not None and ended_at is not None and ended_at > started_at:
        return started_at.astimezone(), ended_at.astimezone()
    base = fallback.astimezone()
    return base, base + _MARKER
