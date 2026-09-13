"""Rachio Public API client for the setup wizard.

At wizard time we can auto-populate each zone's full-refill runtime and refill
depth (and capture the zone's Rachio UUID, which enables the scheduler's live
runtime pull) instead of asking the user to type them. This is purely additive:
every entry point degrades to the manual form if the key is missing or the API
is unreachable.

`parse_zones` and `resolve_secret_text` are pure (unit-tested here). The async
fetch and secret read touch I/O and are covered via the config-flow tests.
"""
from __future__ import annotations

from typing import Any

import yaml

RACHIO_BASE = "https://api.rach.io/1/public/"
_HTTP_TIMEOUT_S = 15


def parse_devices(devices_json: list) -> list[dict]:
    """Map the account's Rachio devices to ``{id, name}`` for the picker.

    Devices without an `id` are skipped so a partial payload degrades to fewer
    entries rather than raising.
    """
    out: list[dict] = []
    for device in devices_json:
        device_id = device.get("id")
        if not device_id:
            continue
        out.append({"id": device_id, "name": device.get("name", "")})
    return out


def parse_zones(zones_json: list) -> list[dict]:
    """Map a Rachio device `zones` array to the fields the wizard needs.

    Returns one dict per enabled zone that has an `id`:
    ``{id, name, zoneNumber, runtime_minutes, refill_depth_mm}``. A zone with an
    `id` but a garbled `runtime`/`depthOfWater` still lists, with ``None`` for
    the value that could not be parsed, so the user can still pick it and fill
    the number by hand. Zones without an `id`, or explicitly ``enabled: False``,
    are skipped.
    """
    out: list[dict] = []
    for zone in zones_json:
        zone_id = zone.get("id")
        if not zone_id or zone.get("enabled") is False:
            continue
        out.append(
            {
                "id": zone_id,
                "name": zone.get("name", ""),
                "zoneNumber": zone.get("zoneNumber"),
                "runtime_minutes": _to_float(zone.get("runtime"), divide_by=60.0),
                "refill_depth_mm": _to_float(zone.get("depthOfWater"), multiply_by=25.4),
            }
        )
    return out


def _to_float(value: Any, *, divide_by: float = 1.0, multiply_by: float = 1.0):
    if value is None:
        return None
    try:
        return float(value) / divide_by * multiply_by
    except (TypeError, ValueError):
        return None


def resolve_secret_text(text: str, name: str) -> str | None:
    """Return the string value of secret `name` from secrets.yaml `text`.

    Returns ``None`` if the file is not a mapping, the key is absent, or the
    value is not a scalar string/number (a list/dict secret is not a usable key).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


async def resolve_secret(hass, name: str) -> str | None:
    """Read <config>/secrets.yaml and resolve secret `name`; None on any failure."""
    path = hass.config.path("secrets.yaml")

    def _read() -> str | None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    text = await hass.async_add_executor_job(_read)
    if text is None:
        return None
    return resolve_secret_text(text, name)


async def _get_json(session, url, key):
    headers = {"Authorization": "Bearer " + key}
    async with session.get(url, headers=headers, timeout=_HTTP_TIMEOUT_S) as resp:
        resp.raise_for_status()
        return await resp.json()


async def async_fetch_devices(session, key: str) -> list[dict]:
    """Fetch the account's Rachio controllers as ``[{id, name}]``.

    Two hops (person/info -> person/{id}). Raises on transport/HTTP/JSON errors;
    the caller treats any failure as "no live data" and falls back to a
    free-text device-name field.
    """
    person = await _get_json(session, RACHIO_BASE + "person/info", key)
    payload = await _get_json(session, RACHIO_BASE + "person/" + person["id"], key)
    return parse_devices(payload.get("devices", []))


async def async_fetch_device_zones(session, key: str, device_id: str) -> list[dict]:
    """Fetch one device's zones, parsed for the wizard (device/{id}).

    Raises on transport/HTTP/JSON errors; the caller falls back to the manual
    zone form.
    """
    payload = await _get_json(session, RACHIO_BASE + "device/" + device_id, key)
    return parse_zones(payload.get("zones", []))
