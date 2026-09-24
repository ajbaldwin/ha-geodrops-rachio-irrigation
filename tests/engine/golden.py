"""Golden (frozen) expected effects for the engine's scenario tests.

Each scenario's expected effects live in `tests/engine/golden/<name>.json`. They
were first captured from the verbatim v0.9.15 pyscript app (the old "legacy
oracle", since deleted), so a passing scenario still means "behaves exactly like
v0.9.15" until a fixture is deliberately regenerated.

Values are stored in a canonical, tagged JSON form so equality keeps Python's
distinctions that plain JSON would erase: a tuple is not a list, a dataclass is
compared field by field under its class name, datetimes keep their offset.

To accept an INTENTIONAL behaviour change, regenerate the affected fixtures from
the native engine and review the diff like any other code change:

    GOLDEN_UPDATE=1 pytest tests/engine -q
    git diff tests/engine/golden
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
from typing import Any

from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"
RECORD_NAMES = ("status", "last_run", "last_nightly", "calibration", "targets",
                "preview", "runtimes")
# The persisted docs a scenario's fixture pins (the ones the v0.9.15 app kept as
# files in its state dir).
STORE_KEYS = tuple(LEGACY_FILE_KEYS.values())


def updating() -> bool:
    return bool(os.environ.get("GOLDEN_UPDATE"))


def encode(obj: Any) -> Any:
    """`obj` as canonical JSON-able data; unknown types are an error, not a
    silent str()."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, tuple):
        return {"$tuple": [encode(v) for v in obj]}
    if isinstance(obj, list):
        return [encode(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [encode(v) for v in obj]
        return {"$set": sorted(items, key=lambda v: json.dumps(v, sort_keys=True))}
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj):
            return {k: encode(v) for k, v in obj.items()}
        pairs = [[encode(k), encode(v)] for k, v in obj.items()]
        return {"$dict": sorted(pairs, key=lambda p: json.dumps(p, sort_keys=True))}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        cls = type(obj)
        return {"$dataclass": f"{cls.__module__}.{cls.__qualname__}",
                "fields": {f.name: encode(getattr(obj, f.name))
                           for f in dataclasses.fields(obj)}}
    if isinstance(obj, dt.datetime):
        return {"$datetime": obj.isoformat()}
    if isinstance(obj, dt.date):
        return {"$date": obj.isoformat()}
    if isinstance(obj, dt.time):
        return {"$time": obj.isoformat()}
    if isinstance(obj, dt.timedelta):
        return {"$timedelta": obj.total_seconds()}
    raise TypeError(f"golden: cannot encode {type(obj).__name__}: {obj!r}")


def _path(name: str) -> pathlib.Path:
    return GOLDEN_DIR / f"{name}.json"


def write(name: str, effects: dict) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(encode(effects), indent=1, sort_keys=True,
                               ensure_ascii=False) + "\n", encoding="utf-8")


def check(name: str, effects: dict) -> None:
    """Assert `effects` equal the stored fixture, key by key (so a failure names
    the part that moved). With GOLDEN_UPDATE set, rewrite the fixture instead."""
    if updating():
        write(name, effects)
        return
    expected = json.loads(_path(name).read_text(encoding="utf-8"))
    actual = encode(effects)
    assert sorted(actual) == sorted(expected), name
    for key in expected:
        assert actual[key] == expected[key], f"{name}: {key}"


def engine_effects(world, eng, log_trail: list) -> dict:
    """The observable effects of a native run: every service call, the status
    transitions, the published records, the persisted docs, and the log trail."""
    return {
        "calls": [list(c) for c in world.calls],
        "status_trail": [[v, a.get("detail")] for n, v, a in eng.record_history
                         if n == "status"],
        "records": {n: [r["value"], r["attributes"]] for n in RECORD_NAMES
                    if (r := eng.records.get(n)) is not None},
        "store": {k: eng.store.read(k) for k in STORE_KEYS},
        "logs": [list(entry) for entry in log_trail],
    }
