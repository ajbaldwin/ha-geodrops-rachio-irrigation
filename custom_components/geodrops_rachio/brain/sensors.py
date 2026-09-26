"""Read a zone's GeoDrops signals and decide online/offline. Pure Python."""
from __future__ import annotations

from dataclasses import dataclass

from .config import ZoneConfig, enum_key, state_rank

_UNAVAILABLE = {"unknown", "unavailable", "none", ""}

# Quality values (as enum keys, see config.enum_key) that still count as a
# usable depth. Bad / Training / unknown are excluded; Poor is accepted
# (GeoDrops reports Poor routinely).
_USABLE_QUALITY = {"good", "poor"}


@dataclass(frozen=True)
class ZoneSignals:
    dominant: str
    state: str
    qualities: tuple[str, str, str]


@dataclass(frozen=True)
class ZoneReading:
    key: str
    online: bool
    offline_reason: str | None  # None when online
    dominant: float | None  # None when offline
    index_rank: int | None  # None when offline
    state_text: str | None  # None when offline


def _offline(key: str, reason: str) -> ZoneReading:
    return ZoneReading(
        key=key, online=False, offline_reason=reason,
        dominant=None, index_rank=None, state_text=None,
    )


def read_zone(zone: ZoneConfig, signals: ZoneSignals) -> ZoneReading:
    dom_raw = (signals.dominant or "").strip()
    state_raw = (signals.state or "").strip()

    if dom_raw.lower() in _UNAVAILABLE or state_raw.lower() in _UNAVAILABLE:
        return _offline(zone.key, "unavailable")

    try:
        dominant = float(dom_raw)
    except ValueError:
        return _offline(zone.key, "unavailable")

    # A depth is USABLE if it reports Good or Poor; only Bad / Training /
    # Unknown disqualify it. Requiring a majority of Good proved far too strict
    # against real GeoDrops data — a single sensor reading Poor on two depths
    # knocked out both zones it feeds. A zone goes offline only when fewer than
    # 2 of 3 depths are usable.
    # (pyscript has no generator expressions; use a list comprehension.)
    usable = sum([1 for q in signals.qualities if enum_key(q) in _USABLE_QUALITY])
    if usable < 2:
        return _offline(zone.key, "low_quality")

    return ZoneReading(
        key=zone.key, online=True, offline_reason=None,
        dominant=dominant, index_rank=state_rank(state_raw), state_text=state_raw,
    )


def all_training(qualities) -> bool:
    """True when all three depths report Training: the probe is still learning
    the site, so a reading taken now says nothing about the water it got."""
    keys = [enum_key(q) for q in qualities]
    return len(keys) == 3 and all([k == "training" for k in keys])
