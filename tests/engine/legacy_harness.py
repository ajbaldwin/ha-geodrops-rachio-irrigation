"""Run the legacy pyscript app (tests/legacy/geodrops_rachio_legacy.py) in CPython.

The app is ordinary Python apart from the names pyscript injects (`state`,
`service`, `task`, `log`, `logbook` and the trigger decorators). We exec it in a
namespace that supplies those names backed by a FakeWorld, then swap its file,
config and Rachio-HTTP helpers for in-memory fakes. The result is the reference
("oracle") the native engine is compared against.
"""
from __future__ import annotations

import copy
import importlib
import pathlib
import sys

from custom_components.geodrops_rachio.engine.base import STATUS_ENTITY

LEGACY_PATH = pathlib.Path(__file__).parents[1] / "legacy" / "geodrops_rachio_legacy.py"
_BRAIN_MODULES = (
    "abort", "blocks", "calibration", "config", "dosing", "drought", "evaluate",
    "plan", "program", "rachio_runtime", "recovery", "report_format", "sensors",
    "weather",
)
# Legacy logbook targets -> the native equivalents (spec: "Entities").
LEGACY_ENTITY_MAP = {
    "pyscript.geodrops_rachio_status": STATUS_ENTITY,
    "pyscript.geodrops_rachio_calibration": STATUS_ENTITY,
}


def _alias_brain() -> None:
    """Make `import geodrops_rachio_lib.X` resolve to the SAME module objects the
    native engine uses, so dataclasses from either side compare equal.

    Force the assignment (not setdefault): tests/test_config_roundtrip.py adds
    CC/bundled_app to sys.path and imports the REAL bundled geodrops_rachio_lib
    package at module scope, which pytest loads during collection — before any
    test body runs. If that import wins the race, `sys.modules["geodrops_rachio_lib"]`
    already holds the bundled copy by the time this first runs, and a setdefault
    would silently keep it, giving the legacy exec a *different* Config/Tunables/
    etc. class than the native engine's — so `==` on any dataclass instance built
    from each side (e.g. `_plan_context`'s "cfg"/"tun") is always False despite
    identical field values. test_config_roundtrip.py binds its own `scheduler_config`
    name at import time, so overwriting the sys.modules entry afterward does not
    affect it.
    """
    pkg = importlib.import_module("custom_components.geodrops_rachio.brain")
    sys.modules["geodrops_rachio_lib"] = pkg
    for name in _BRAIN_MODULES:
        mod = importlib.import_module(f"custom_components.geodrops_rachio.brain.{name}")
        sys.modules[f"geodrops_rachio_lib.{name}"] = mod


class LegacyFiles:
    """The legacy state dir, in memory, keyed by file basename."""

    def __init__(self) -> None:
        self.files: dict[str, object] = {}

    def read(self, path):
        return copy.deepcopy(self.files.get(pathlib.PurePosixPath(path).name))

    def write(self, path, data):
        self.files[pathlib.PurePosixPath(path).name] = copy.deepcopy(data)

    def delete(self, path):
        self.files.pop(pathlib.PurePosixPath(path).name, None)


class _State:
    def __init__(self, world):
        self.w = world

    def get(self, name):
        if name.endswith(".last_updated"):
            ent = name[: -len(".last_updated")]
            if not self.w.exists(ent):
                raise NameError(name)
            return self.w.last_updated(ent)
        if not self.w.exists(name):
            raise NameError(name)
        return self.w.get(name)

    def getattr(self, name):
        if not self.w.exists(name):
            raise NameError(name)
        return self.w.attrs(name)

    def set(self, name, value=None, new_attributes=None):
        self.w.publish(name, value, dict(new_attributes or {}))


class _Service:
    def __init__(self, world):
        self.w = world

    def __call__(self, fn):  # the @service decorator
        return fn

    def call(self, domain, name, **kwargs):
        self.w.call(domain, name, kwargs)


class _Task:
    def __init__(self, world):
        self.w = world

    def sleep(self, seconds):
        pending = self.w.advance(seconds)
        assert not pending, "legacy scenario events must be synchronous"

    def executor(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def unique(self, name):
        return None


class _Log:
    def __init__(self, world):
        self.w = world

    def __getattr__(self, level):
        return lambda msg: self.w.logs.append((level, msg))


class _Logbook:
    def __init__(self, world):
        self.w = world

    def log(self, **kwargs):
        self.w.call("logbook", "log", kwargs)


def load_legacy(world, raw_config: dict, files: LegacyFiles | None = None) -> dict:
    _alias_brain()
    files = files if files is not None else LegacyFiles()
    ns = {
        "__name__": "geodrops_rachio_legacy",
        "state": _State(world), "service": _Service(world), "task": _Task(world),
        "log": _Log(world), "logbook": _Logbook(world),
        "pyscript_compile": lambda fn: fn,
        "time_trigger": lambda *a, **k: (lambda fn: fn),
        "state_trigger": lambda *a, **k: (lambda fn: fn),
    }
    source = LEGACY_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(LEGACY_PATH), "exec"), ns)
    ns["_read_json"] = files.read
    ns["_write_json_atomic"] = files.write
    ns["_delete_file"] = files.delete
    ns["_read_config_yaml"] = lambda _path: copy.deepcopy(raw_config)

    def _fetch_zone_data():
        if world.rachio_api is None:
            raise RuntimeError("rachio api down")
        return copy.deepcopy(world.rachio_api)

    ns["_fetch_zone_data"] = _fetch_zone_data
    ns["__files__"] = files
    return ns
