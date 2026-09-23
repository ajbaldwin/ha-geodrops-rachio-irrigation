"""Helpers for differential tests: legacy app vs native engine."""
from __future__ import annotations

from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.legacy_harness import LEGACY_ENTITY_MAP

RECORD_NAMES = ("status", "last_run", "last_nightly", "calibration", "targets",
                "preview", "runtimes")


def prime(target, cfg) -> None:
    """Install a loaded config the way _plan_and_run does (legacy ns or engine)."""
    if isinstance(target, dict):
        target["_current_cfg"] = cfg
        target["_current_bindings"] = cfg.bindings
        target["_current_tun"] = cfg.tunables
    else:
        target._current_cfg = cfg
        target._current_bindings = cfg.bindings
        target._current_tun = cfg.tunables


def legacy_calls(world) -> list:
    out = []
    for domain, service, data in world.calls:
        if domain == "logbook":
            ent = data.get("entity_id")
            data = {**data, "entity_id": LEGACY_ENTITY_MAP.get(ent, ent)}
        out.append((domain, service, data))
    return out


def status_trail_legacy(world) -> list:
    return [(v, a.get("detail")) for e, v, a in world.published_history
            if e == "pyscript.geodrops_rachio_status"]


def status_trail_native(engine) -> list:
    return [(v, a.get("detail")) for n, v, a in engine.record_history if n == "status"]


def assert_same_effects(lw, lfiles, nw, eng) -> None:
    assert legacy_calls(lw) == nw.calls
    assert status_trail_legacy(lw) == status_trail_native(eng)
    for name in RECORD_NAMES:
        legacy = lw.published.get(f"pyscript.geodrops_rachio_{name}")
        native = eng.records.get(name)
        if legacy is None:
            assert native is None, name
        else:
            assert native is not None, name
            assert (native["value"], native["attributes"]) == legacy, name
    for fname, key in LEGACY_FILE_KEYS.items():
        assert lfiles.files.get(fname) == eng.store.read(key), fname
