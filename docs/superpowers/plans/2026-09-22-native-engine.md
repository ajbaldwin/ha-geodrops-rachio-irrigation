# Native Engine (Remove pyscript) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the irrigation scheduler natively inside the `geodrops_rachio` integration so it no longer needs pyscript, with behaviour proven identical to the legacy pyscript app.

**Architecture:** The pyscript app (`bundled_app/geodrops_rachio.py`) is ported function-by-function into async mixin classes under `engine/`, talking to Home Assistant only through an `HAPort`. The pure logic moves unchanged to `brain/`. The legacy app is kept verbatim as a test fixture and executed with fake pyscript globals, so every ported layer is checked by *differential* tests: the same scenario is run on legacy and native against identical simulated worlds, and every service call, status transition, record and persisted document must match.

**Tech Stack:** Python 3.13, Home Assistant custom integration (min HA 2026.3.0), `homeassistant.helpers.storage.Store`, pytest + pytest-homeassistant-custom-component + freezegun (`freezer` fixture), Docker test image `geodrops-test`.

**Spec:** `docs/superpowers/specs/2026-09-22-native-engine-design.md`

## Global Constraints

- Repo: `C:\Users\Adam\Projects\Projects\ghr-native-engine` (worktree of `ajbaldwin/ha-geodrops-rachio-irrigation`), branch `feature/native-engine`. Commit per task. Do not push until Task 13.
- Integration root below is abbreviated `CC` = `custom_components/geodrops_rachio`.
- Min HA stays **2026.3.0**. Target release **v1.0.0**.
- Behaviour is ported **unchanged**. The only behaviour change allowed is the unload safety stop (Task 11). If a differential test fails, fix the port — never edit the legacy fixture or loosen the assertion.
- The legacy app source is `CC/bundled_app/geodrops_rachio.py` until Task 12 deletes it; from Task 4 on it also lives verbatim at `tests/legacy/geodrops_rachio_legacy.py`. Line numbers in this plan refer to that file (identical content).
- Service calls from the engine are non-blocking (`blocking=False`), matching pyscript's `service.call` default — except the unload safety stop (blocking, 10 s bound).
- Logbook entries previously attached to `pyscript.geodrops_rachio_status` or `pyscript.geodrops_rachio_calibration` attach to `sensor.geodrops_rachio_status`.
- New entities: `sensor.geodrops_rachio_last_nightly`, `sensor.geodrops_rachio_last_run`, `sensor.geodrops_rachio_plan` (record value as state, record attributes minus `friendly_name`, `_unrecorded_attributes = frozenset({MATCH_ALL})`). `sensor.geodrops_rachio_status` gains a raw `status` attribute.
- Store: one `Store` per entry, key `geodrops_rachio.<entry_id>`, data `{"docs": {...}}`. Persisted docs: `efficacy`, `pending_obs`, `waiting_marker`, `record.last_nightly`, `record.calibration`, `record.targets`, `record.preview`.
- Test commands:
  - Brain (Windows native OK): `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain -q`
  - Everything else (Docker Desktop must be running): `bash tools/test.sh <args>` (runs `pytest <args>` in the `geodrops-test` image)
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Files in this worktree cannot be written with the Write/Edit tools from a session anchored elsewhere (hook). Run the implementation from a session whose working directory is the worktree.

### Port rules (apply to every ported function in Tasks 6–11)

Port each legacy function to a method of the **same name** on the named mixin. Keep every comment, docstring, log/activity message string, attribute key and control-flow branch. Delete only `global` statements and docstring sentences that explain pyscript limitations.

| Legacy | Native |
|---|---|
| function that (transitively) calls `service.call`, `logbook.log`, `task.sleep`, a store write, or another async method | `async def name(self, ...)`; every call site `await self.name(...)` |
| function that only reads state / computes | plain `def name(self, ...)` |
| `state.get(e)` | `self.port.state(e)` — returns `None` when the entity does not exist |
| `try: x = state.get(e)` / `except NameError: <fallback>` | `x = self.port.state(e)` then `if x is None: <fallback>` (same warning text) |
| `state.get(e + ".last_updated")` | `self.port.last_updated(e)` |
| `state.getattr("pyscript.geodrops_rachio_last_nightly")` | `(self.records.get("last_nightly") or {}).get("attributes")` (None when absent → same path as the legacy `NameError`) |
| `state.set(STATUS_ENTITY, value=v, new_attributes=a)` | `self._publish("status", v, a)` |
| `state.set("pyscript.geodrops_rachio_<n>", value=v, new_attributes=a)` | `self._publish("<n>", v, a)` |
| `_publish_record("geodrops_rachio_<n>", v, a)` | `await self._publish_record("<n>", v, a)` |
| `service.call(d, s, k1=v1, k2=v2)` | `await self.port.call(d, s, {"k1": v1, "k2": v2})` |
| `task.sleep(s)` | `await self.port.sleep(s)` |
| `task.executor(_read_json, EFFICACY_PATH)` / `PENDING_OBS_PATH` / `WAITING_MARKER_PATH` | `self.store.read(EFFICACY)` / `PENDING_OBS` / `WAITING_MARKER` |
| `task.executor(_write_json_atomic, <PATH>, data)` | `await self.store.write(<KEY>, data)` |
| `task.executor(_delete_file, WAITING_MARKER_PATH)` | `await self.store.delete(WAITING_MARKER)` |
| `config.parse_config(task.executor(_read_config_yaml, CONFIG_PATH))` | `self._load_cfg()` |
| `log.warning(f"...")` / `log.info(f"...")` | `_LOGGER.warning(f"...")` / `_LOGGER.info(f"...")` — identical text |
| `dt.datetime.now()` (naive) | `self._naive_now()` |
| `dt.datetime.now().astimezone()` | `self.port.now()` |
| `dt.datetime.now(tz)` | `self.port.now().astimezone(tz)` |
| `time.time()` | `self.port.now().timestamp()` |
| module globals (`_manual_stop`, `api_calls`, `state_polls`, `_runtime_cache`, `_current_tun`, `_current_cfg`, `_current_bindings`, `_run_in_progress`, `_watering_active`, `_rain_since`) | same-named instance attributes (`self._manual_stop`, `self.api_calls`, …) |
| bare-reference predicates passed as callbacks (`_is_standby`, …) | bound methods (`self._is_standby`, …) |
| pyscript-isms (list comprehensions instead of generators, explicit loops) | keep exactly — do not modernise |

Worked example (legacy lines 296–308 → `IOMixin.stop_device`):

```python
# legacy
def stop_device():
    global api_calls
    api_calls += 1
    try:
        service.call("rachio", "stop_watering",
                     devices=_current_bindings.rachio_device_name)
    except Exception as err:
        log.warning(f"irrigation: stop_device (rachio.stop_watering) failed: {err}")

# native
    async def stop_device(self):
        """Stop the whole running-or-paused schedule (device level).

        Toggling a single zone switch off would let Rachio advance to the next zone
        of a collapsed schedule; this ends the schedule outright.
        """
        self.api_calls += 1
        try:
            await self.port.call("rachio", "stop_watering",
                                 {"devices": self._current_bindings.rachio_device_name})
        except Exception as err:
            _LOGGER.warning(f"irrigation: stop_device (rachio.stop_watering) failed: {err}")
```

---

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `CC/brain/*.py` | Pure scheduler logic (copy of `geodrops_rachio_lib`, relative imports) | 1 |
| `CC/config_writer.py` | `build_config(data) -> dict` (YAML emitter removed in Task 12) | 2, 12 |
| `CC/engine/port.py` | `HAPort` protocol + `HassPort` | 3 |
| `CC/engine/store.py` | `EngineStore`, doc keys, legacy import, pyscript retirement | 5 |
| `CC/engine/base.py` | `EngineBase`: former globals, config load, publish/status/activity/notify, listeners | 6 |
| `CC/engine/io.py` | `IOMixin`: Rachio zone I/O, counters, runtime cache, efficacy/pending docs | 6 |
| `CC/engine/runner.py` | `RunnerMixin`: valve loops | 7 |
| `CC/engine/planning.py` | `PlanningMixin`: predicates, reads, `_plan_context`, rain-skip | 8 |
| `CC/engine/orchestration.py` | `OrchestrationMixin`: records, waiting marker, `_plan_and_run`, preview, recap | 9 |
| `CC/engine/learning.py` | `LearningMixin`: 06:00 calibration, settle-and-learn | 10 |
| `CC/engine/scheduler.py` | `Scheduler`: composition, triggers, run task, startup, actions, unload safety | 11 |
| `CC/rachio_client.py` | + `async_fetch_zone_data` | 12 |
| `CC/__init__.py`, `button.py`, `sensor.py`, `coordinator.py`, `config_flow.py`, `manifest.json`, `strings.json`, `translations/en.json` | Wiring | 12 |
| `tests/engine/world.py` | `FakeWorld`, `RachioSim`, `FakePort` | 3 |
| `tests/legacy/geodrops_rachio_legacy.py` | Verbatim legacy app (oracle) | 4 |
| `tests/engine/legacy_harness.py` | Exec the legacy app against a `FakeWorld` | 4 |
| `tests/engine/scenario.py` | Synthetic entry data, world population, engine builders | 4, 6, 11 |
| `tests/engine/diff.py` | Run-both helpers + equivalence assertions | 7 |
| Deleted in Task 12 | `CC/bundled_app/`, `CC/delivery.py`, `CC/updater.py`, `tests/test_delivery.py`, `tests/test_updater.py` | 12 |

---

### Task 1: `brain/` package

**Files:**
- Create: `CC/brain/` (copy of `CC/bundled_app/geodrops_rachio_lib/`)
- Modify: `tests_brain/conftest.py`, every `tests_brain/test_*.py` (import path)

**Interfaces:**
- Produces: package `custom_components.geodrops_rachio.brain` with modules `abort, blocks, calibration, config, dosing, drought, evaluate, plan, program, rachio_runtime, recovery, report_format, sensors, weather` — identical code, relative imports. In `tests_brain` it is importable as top-level `brain`.

- [ ] **Step 1: Copy the lib and switch to relative imports**

```bash
cp -r custom_components/geodrops_rachio/bundled_app/geodrops_rachio_lib custom_components/geodrops_rachio/brain
rm -rf custom_components/geodrops_rachio/brain/__pycache__
sed -i 's/^from geodrops_rachio_lib\./from ./' custom_components/geodrops_rachio/brain/*.py
grep -rn "geodrops_rachio_lib" custom_components/geodrops_rachio/brain/ && echo "LEFTOVER" || echo "clean"
```
Expected: `clean`.

- [ ] **Step 2: Point tests_brain at `brain`**

Replace `tests_brain/conftest.py` with:

```python
"""Test config for the brain (pure-logic) suite.

The scheduler's pure logic lives in custom_components/geodrops_rachio/brain.
Putting the integration directory on sys.path lets these tests import it as a
standalone top-level package (`brain`) without importing Home Assistant, so
they run on any platform:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain`.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(
    0,
    os.path.join(_HERE, "..", "custom_components", "geodrops_rachio"),
)

import pytest  # noqa: E402


@pytest.fixture
def example_config_path():
    return os.path.join(_HERE, "examples", "config.example.yaml")
```

Then:

```bash
sed -i 's/geodrops_rachio_lib/brain/g' tests_brain/test_*.py
grep -rn "geodrops_rachio_lib\|bundled_app" tests_brain/ && echo "LEFTOVER" || echo "clean"
```
Expected: `clean`.

- [ ] **Step 3: Run the brain suite natively**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain -q`
Expected: `279 passed` (same count as before the change).

- [ ] **Step 4: Run the full suite in Docker**

Run: `bash tools/test.sh -q`
Expected: all pass (bundled_app is untouched, so delivery tests still pass).

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/brain tests_brain
git commit -m "refactor: add brain package (pure scheduler logic, relative imports)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `config_writer.build_config`

**Files:**
- Modify: `CC/config_writer.py:77-119`
- Test: `tests/test_config_writer.py`

**Interfaces:**
- Produces: `build_config(data: dict) -> dict` — the config document (keys `homeassistant, tunables, bands, drought_profiles, zones`); raises `ValueError` on bad `advanced_overrides`. `generate_config(data) -> str` stays (header + YAML dump of `build_config`) until Task 12.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config_writer.py`:

```python
def test_build_config_matches_generated_yaml():
    from custom_components.geodrops_rachio.config_writer import build_config
    assert build_config(BASE) == yaml.safe_load(generate_config(BASE))


def test_build_config_parses_with_brain():
    from custom_components.geodrops_rachio.brain import config as brain_config
    from custom_components.geodrops_rachio.config_writer import build_config
    cfg = brain_config.parse_config(build_config(BASE))
    assert cfg.zones["front"].rachio_switch == "switch.front"
    assert cfg.bindings.notify_service == "notify.phone"


def test_build_config_returns_fresh_dict_each_call():
    from custom_components.geodrops_rachio.config_writer import build_config
    first = build_config(BASE)
    first["zones"]["front"]["rachio_switch"] = "mutated"
    assert build_config(BASE)["zones"]["front"]["rachio_switch"] == "switch.front"
```

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/test_config_writer.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_config'`.

- [ ] **Step 3: Split `generate_config`** — rename the existing function body to `build_config`, ending with `return doc` instead of the YAML dump, and add:

```python
def generate_config(data: dict) -> str:
    return GENERATED_HEADER + yaml.safe_dump(
        build_config(data), sort_keys=False, default_flow_style=False)
```

- [ ] **Step 4: Run tests**

Run: `bash tools/test.sh tests/test_config_writer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/config_writer.py tests/test_config_writer.py
git commit -m "refactor(config_writer): build_config returns the config dict

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `HAPort` + simulated world

**Files:**
- Create: `CC/engine/__init__.py`, `CC/engine/port.py`
- Create: `tests/engine/__init__.py`, `tests/engine/world.py`
- Test: `tests/engine/test_world.py`, `tests/engine/test_port.py`

**Interfaces:**
- Produces (`engine/port.py`):
  - `class HAPort(Protocol)`: `state(entity_id) -> str | None`, `attrs(entity_id) -> dict`, `last_updated(entity_id) -> datetime | None`, `async call(domain, service, data: dict, *, blocking=False) -> None`, `async sleep(seconds: float) -> None`, `now() -> datetime` (tz-aware local).
  - `class HassPort(hass)` implementing it.
- Produces (`tests/engine/world.py`):
  - `FakeWorld(freezer)`: `now()`, `exists(e)`, `get(e)`, `attrs(e)`, `last_updated(e)`, `set(e, value, attributes=None)`, `publish(e, value, attributes)` (legacy `state.set`), `published: dict[e, (value, attrs)]`, `published_history: list[(e, value, attrs)]`, `call(domain, service, data)`, `calls: list[(domain, service, data)]`, `logs: list[(level, msg)]`, `failing: set[(domain, service)]`, `at(when_str_or_dt, fn)`, `advance(seconds) -> list[coroutine]`, `rachio: RachioSim`, `rachio_api: tuple[dict, dict, dict] | None`.
  - `RachioSim`: `zone_switches: set[str]`, `running_switch()`, `drop_at: datetime | None`, `refuse_next_start: bool`, `start_direct(pairs: list[tuple[str, int]])` (test helper: an orphan schedule).
  - `FakePort(world)` implementing `HAPort`.

- [ ] **Step 1: Write the port**

`CC/engine/__init__.py`:
```python
"""Native irrigation engine (the former pyscript app)."""
```

`CC/engine/port.py`:
```python
"""The engine's only window onto Home Assistant.

Every read of HA state, service call, sleep and clock read the engine makes goes
through an HAPort, so the same engine code runs against a live Home Assistant
(HassPort) and against the simulated world the tests use (tests/engine/world.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


class HAPort(Protocol):
    def state(self, entity_id: str) -> str | None: ...

    def attrs(self, entity_id: str) -> dict: ...

    def last_updated(self, entity_id: str) -> dt.datetime | None: ...

    async def call(self, domain: str, service: str, data: dict, *,
                   blocking: bool = False) -> None: ...

    async def sleep(self, seconds: float) -> None: ...

    def now(self) -> dt.datetime: ...


class HassPort:
    """HAPort over a live HomeAssistant instance."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def state(self, entity_id: str) -> str | None:
        st = self.hass.states.get(entity_id)
        return st.state if st is not None else None

    def attrs(self, entity_id: str) -> dict:
        st = self.hass.states.get(entity_id)
        return dict(st.attributes) if st is not None else {}

    def last_updated(self, entity_id: str) -> dt.datetime | None:
        st = self.hass.states.get(entity_id)
        return st.last_updated if st is not None else None

    async def call(self, domain: str, service: str, data: dict, *,
                   blocking: bool = False) -> None:
        await self.hass.services.async_call(domain, service, data, blocking=blocking)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def now(self) -> dt.datetime:
        return dt_util.now()
```

- [ ] **Step 2: Write the world** — `tests/engine/__init__.py` empty; `tests/engine/world.py`:

```python
"""A simulated Home Assistant + Rachio controller for engine tests.

Both the native engine (through FakePort) and the legacy pyscript app (through
legacy_harness) run against a FakeWorld, so one scenario can be replayed on each
and the observable effects compared.

Time is the freezegun clock (the `freezer` fixture): nothing sleeps for real.
`advance()` moves the clock, firing scripted events in time order. Rachio zone
switch states are derived lazily from the clock, so a multi-hour night costs no
per-second stepping.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import heapq
import inspect
from typing import Any, Callable


def _aware(when) -> dt.datetime:
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when)
    return when if when.tzinfo else when.astimezone()


class RachioSim:
    """One Rachio controller running at most one multi-zone schedule.

    `queue` is the schedule as (switch, seconds) pairs. `banked` is watered
    seconds accumulated before `since`, the start of the current un-paused
    stretch. While paused, `paused_until` is when Rachio auto-resumes on its own
    (HA's pause_watering duration).
    """

    def __init__(self, world: "FakeWorld") -> None:
        self.world = world
        self.zone_switches: set[str] = set()
        self.queue: list[tuple[str, float]] = []
        self.banked = 0.0
        self.since: dt.datetime | None = None
        self.paused_until: dt.datetime | None = None
        self.drop_at: dt.datetime | None = None
        self.refuse_next_start = False

    def _total(self) -> float:
        return sum([s for _sw, s in self.queue])

    def _clear(self) -> None:
        self.queue = []
        self.banked = 0.0
        self.since = None
        self.paused_until = None

    def _dropped(self, now: dt.datetime) -> bool:
        return self.drop_at is not None and now >= self.drop_at

    def _watered(self, now: dt.datetime) -> float:
        if not self.queue:
            return 0.0
        if self.paused_until is not None:
            if now < self.paused_until:
                return self.banked
            return self.banked + (now - self.paused_until).total_seconds()
        return self.banked + (now - self.since).total_seconds()

    def _materialize(self) -> None:
        """Fold elapsed time into `banked` so a command acts from 'now'."""
        now = self.world.now()
        if self._dropped(now):
            self._clear()
            self.drop_at = None
            return
        if not self.queue:
            return
        self.banked = self._watered(now)
        self.since = now
        self.paused_until = None
        if self.banked >= self._total():
            self._clear()

    def running_switch(self) -> str | None:
        now = self.world.now()
        if not self.queue or self._dropped(now):
            return None
        if self.paused_until is not None and now < self.paused_until:
            return None
        t = self._watered(now)
        for sw, secs in self.queue:
            if t < secs:
                return sw
            t -= secs
        return None

    def start_direct(self, pairs: list[tuple[str, int]]) -> None:
        """Test helper: a schedule already running (e.g. an orphan after a crash)."""
        self.queue = [(sw, m * 60.0) for sw, m in pairs]
        self.banked = 0.0
        self.since = self.world.now()
        self.paused_until = None

    def handle(self, domain: str, service: str, data: dict) -> None:
        if domain == "rachio" and service == "start_multiple_zone_schedule":
            self._materialize()
            if self.refuse_next_start:
                self.refuse_next_start = False
                return
            ids = list(data["entity_id"])
            mins = [int(m) for m in str(data["duration"]).split(",")]
            self.start_direct(list(zip(ids, mins)))
        elif domain == "rachio" and service == "pause_watering":
            self._materialize()
            if self.queue:
                self.paused_until = self.world.now() + dt.timedelta(
                    minutes=int(data["duration"]))
        elif domain == "rachio" and service == "resume_watering":
            self._materialize()
        elif domain == "rachio" and service == "stop_watering":
            self._clear()
        elif (domain == "switch" and service == "turn_off"
              and data.get("entity_id") in self.zone_switches):
            self._materialize()
            if self.running_switch() == data["entity_id"]:
                # Rachio advances to the next zone: skip the rest of this one.
                acc = 0.0
                for _sw, secs in self.queue:
                    if self.banked < acc + secs:
                        self.banked = acc + secs
                        break
                    acc += secs
                if self.banked >= self._total():
                    self._clear()


class FakeWorld:
    def __init__(self, freezer) -> None:
        self.freezer = freezer
        self._states: dict[str, Any] = {}
        self._attrs: dict[str, dict] = {}
        self._updated: dict[str, dt.datetime] = {}
        self.published: dict[str, tuple[Any, dict]] = {}
        self.published_history: list[tuple[str, Any, dict]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.logs: list[tuple[str, str]] = []
        self.failing: set[tuple[str, str]] = set()
        self._events: list = []
        self._seq = 0
        self.rachio = RachioSim(self)
        # (runtimes, depths, spans) the Rachio cloud returns; None = API down.
        self.rachio_api: tuple[dict, dict, dict] | None = ({}, {}, {})

    # --- clock -------------------------------------------------------------
    def now(self) -> dt.datetime:
        return dt.datetime.now().astimezone()

    def at(self, when, fn: Callable[[], Any]) -> None:
        self._seq += 1
        heapq.heappush(self._events, (_aware(when), self._seq, fn))

    def advance(self, seconds: float) -> list:
        """Move the clock forward, firing due events in order. Returns the
        coroutines events produced (the native engine awaits them; legacy events
        run synchronously and return None)."""
        target = self.now() + dt.timedelta(seconds=seconds)
        pending = []
        while self._events and self._events[0][0] <= target:
            when, _seq, fn = heapq.heappop(self._events)
            if when > self.now():
                self.freezer.move_to(when)
            out = fn()
            if inspect.isawaitable(out):
                pending.append(out)
        self.freezer.move_to(target)
        return pending

    # --- state machine -----------------------------------------------------
    def exists(self, entity_id: str) -> bool:
        return entity_id in self._states or entity_id in self.rachio.zone_switches

    def get(self, entity_id: str):
        if entity_id in self.rachio.zone_switches:
            return "on" if self.rachio.running_switch() == entity_id else "off"
        return self._states.get(entity_id)

    def attrs(self, entity_id: str) -> dict:
        return dict(self._attrs.get(entity_id, {}))

    def last_updated(self, entity_id: str) -> dt.datetime | None:
        return self._updated.get(entity_id)

    def set(self, entity_id: str, value, attributes: dict | None = None) -> None:
        if self._states.get(entity_id) != value or entity_id not in self._updated:
            self._updated[entity_id] = self.now()
        self._states[entity_id] = value
        if attributes is not None:
            self._attrs[entity_id] = dict(attributes)

    def remove(self, entity_id: str) -> None:
        self._states.pop(entity_id, None)
        self._attrs.pop(entity_id, None)
        self._updated.pop(entity_id, None)

    def publish(self, entity_id: str, value, attributes: dict) -> None:
        """Legacy pyscript `state.set` of its own pyscript.* entity."""
        attributes = copy.deepcopy(attributes)
        self.set(entity_id, value, attributes)
        self.published[entity_id] = (value, attributes)
        self.published_history.append((entity_id, value, attributes))

    # --- services ----------------------------------------------------------
    def call(self, domain: str, service: str, data: dict) -> None:
        data = copy.deepcopy(dict(data))
        self.calls.append((domain, service, data))
        if (domain, service) in self.failing:
            raise RuntimeError(f"{domain}.{service} failed")
        self.rachio.handle(domain, service, data)
        entity = data.get("entity_id")
        if (domain == "switch" and isinstance(entity, str)
                and entity not in self.rachio.zone_switches
                and service in ("turn_on", "turn_off")):
            self.set(entity, "on" if service == "turn_on" else "off")


class FakePort:
    """HAPort over a FakeWorld (the native engine's view in tests)."""

    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def state(self, entity_id):
        return self.world.get(entity_id)

    def attrs(self, entity_id):
        return self.world.attrs(entity_id)

    def last_updated(self, entity_id):
        return self.world.last_updated(entity_id)

    async def call(self, domain, service, data, *, blocking=False):
        self.world.call(domain, service, data)

    async def sleep(self, seconds):
        for coro in self.world.advance(seconds):
            await coro
        await asyncio.sleep(0)

    def now(self):
        return self.world.now()
```

- [ ] **Step 3: Write the world tests** — `tests/engine/test_world.py`:

```python
import asyncio
import datetime as dt

from tests.engine.world import FakePort, FakeWorld


def _world(freezer):
    freezer.move_to("2026-07-02 02:00:00")
    w = FakeWorld(freezer)
    w.rachio.zone_switches |= {"switch.a", "switch.b"}
    return w


def test_schedule_walks_zones_in_order(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a", "switch.b"], "duration": "10,5"})
    assert w.get("switch.a") == "on" and w.get("switch.b") == "off"
    w.advance(10 * 60 + 1)
    assert w.get("switch.a") == "off" and w.get("switch.b") == "on"
    w.advance(5 * 60)
    assert w.get("switch.b") == "off"


def test_pause_holds_then_auto_resumes(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    w.advance(60)
    w.call("rachio", "pause_watering", {"devices": "Main", "duration": 2})
    assert w.get("switch.a") == "off"
    w.advance(121)
    assert w.get("switch.a") == "on"          # auto-resumed after 2 min
    w.advance(9 * 60)                         # 1 + 9 = 10 min watered
    assert w.get("switch.a") == "off"


def test_explicit_resume_and_stop(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    w.call("rachio", "pause_watering", {"devices": "Main", "duration": 60})
    w.advance(30)
    w.call("rachio", "resume_watering", {"devices": "Main"})
    assert w.get("switch.a") == "on"
    w.call("rachio", "stop_watering", {"devices": "Main"})
    assert w.get("switch.a") == "off"


def test_zone_turn_off_advances_to_next_zone(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a", "switch.b"], "duration": "10,5"})
    w.call("switch", "turn_off", {"entity_id": "switch.a"})
    assert w.get("switch.b") == "on"


def test_drop_and_refused_start(freezer):
    w = _world(freezer)
    w.rachio.refuse_next_start = True
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    assert w.get("switch.a") == "off"
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    w.rachio.drop_at = w.now() + dt.timedelta(minutes=3)
    w.advance(179)
    assert w.get("switch.a") == "on"
    w.advance(2)
    assert w.get("switch.a") == "off"


def test_events_fire_in_order_and_return_coroutines(freezer):
    w = _world(freezer)
    seen = []

    async def later():
        seen.append("coro")

    w.at("2026-07-02 02:05:00", lambda: seen.append("sync"))
    w.at("2026-07-02 02:03:00", later)
    pending = w.advance(600)
    assert seen == ["sync"] and len(pending) == 1
    asyncio.run(pending[0])
    assert seen == ["sync", "coro"]


def test_plain_switch_service_updates_state_and_failing(freezer):
    w = _world(freezer)
    w.call("switch", "turn_on", {"entity_id": "switch.run_active"})
    assert w.get("switch.run_active") == "on"
    w.failing.add(("notify", "phone"))
    try:
        w.call("notify", "phone", {"message": "x"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected failure")


async def test_fake_port_sleep_awaits_event_coroutines(freezer):
    w = _world(freezer)
    seen = []

    async def ev():
        seen.append(w.now().strftime("%H:%M"))

    w.at("2026-07-02 02:01:00", ev)
    await FakePort(w).sleep(120)
    assert seen == ["02:02"]  # awaited after the clock reached the target
```

`tests/engine/test_port.py`:
```python
from custom_components.geodrops_rachio.engine.port import HassPort


async def test_hass_port_reads_and_calls(hass):
    port = HassPort(hass)
    assert port.state("sensor.nope") is None
    assert port.attrs("sensor.nope") == {}
    hass.states.async_set("sensor.x", "42", {"unit": "%"})
    assert port.state("sensor.x") == "42"
    assert port.attrs("sensor.x") == {"unit": "%"}
    assert port.last_updated("sensor.x") is not None

    calls = []
    hass.services.async_register("test", "echo", lambda call: calls.append(call.data))
    await port.call("test", "echo", {"a": 1}, blocking=True)
    assert calls == [{"a": 1}]
    assert port.now().tzinfo is not None
```

- [ ] **Step 4: Run**

Run: `bash tools/test.sh tests/engine -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/engine tests/engine
git commit -m "test(engine): HAPort + simulated HA/Rachio world

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Legacy oracle harness

**Files:**
- Create: `tests/legacy/geodrops_rachio_legacy.py` (verbatim copy)
- Create: `tests/engine/legacy_harness.py`, `tests/engine/scenario.py`
- Test: `tests/engine/test_legacy_harness.py`

**Interfaces:**
- Consumes: `FakeWorld` (Task 3), `build_config` (Task 2), `brain` (Task 1).
- Produces (`legacy_harness.py`): `LegacyFiles` (`files: dict[basename, obj]`, `read(path)`, `write(path, data)`, `delete(path)`), `load_legacy(world, raw_config: dict, files: LegacyFiles | None = None) -> dict` (the executed namespace; the legacy functions are `ns["_plan_and_run"]`, `ns["_on_startup"]`, …), `LEGACY_ENTITY_MAP: dict[str, str]`.
- Produces (`scenario.py`): `ZONES = ("front", "back")`, `entry_data(*, self_cal=False, overrides="") -> dict`, `zone_data(key, geography) -> dict`, `populate(world, data, *, level="Level 1 - Mild", dominant="60.0") -> None`, `T_PLAN = "2026-07-01 23:00:00"`.

- [ ] **Step 1: Copy the legacy app**

```bash
mkdir -p tests/legacy
cp custom_components/geodrops_rachio/bundled_app/geodrops_rachio.py tests/legacy/geodrops_rachio_legacy.py
```

Add `tests/legacy/README.md`:
```markdown
`geodrops_rachio_legacy.py` is the pyscript app exactly as shipped in v0.9.15.
It is the oracle for the differential tests in `tests/engine/` and must never be
edited. Delete this directory in the first release after v1.0.0.
```

- [ ] **Step 2: Write the scenario fixtures** — `tests/engine/scenario.py`:

```python
"""Synthetic install used by every engine scenario (no real yard data)."""
from __future__ import annotations

from tests.engine.world import FakeWorld

T_PLAN = "2026-07-01 23:00:00"
ZONES = ("front", "back")


def zone_data(key: str, geography: str) -> dict:
    return {
        "key": key, "rachio_switch": f"switch.{key}_zone",
        "dominant_sensor": f"sensor.{key}_dominant",
        "state_sensor": f"sensor.{key}_state",
        "quality_sensors": [f"sensor.{key}_q1", f"sensor.{key}_q2", f"sensor.{key}_q3"],
        "target_range": "moist", "geography": geography, "adjacency": [],
        "runtime_minutes": 40, "rachio_zone_id": f"id-{key}",
        "refill_depth_mm": 7.5, "spray": False,
    }


def entry_data(*, self_cal: bool = False, overrides: str = "") -> dict:
    return {
        "bindings": {
            "notify_service": "notify.phone",
            "calendar_entity": "calendar.lawn",
            "rachio_device_name": "Main House",
            "rachio_api_key_secret": "rachio_api_key",
            "drought_level_select": "select.geodrops_rachio_drought_level",
            "standby_boolean": "switch.geodrops_rachio_standby",
            "standby_switch": "switch.rachio_standby",
            "dew_formed_boolean": "switch.geodrops_rachio_dew_formed",
            "run_active_boolean": "switch.geodrops_rachio_run_active",
        },
        "zones": [zone_data("front", "front"), zone_data("back", "back")],
        "self_calibration_enabled": self_cal,
        "advanced_overrides": overrides,
    }


def populate(world: FakeWorld, data: dict, *, level: str = "Level 1 - Mild",
             dominant: str = "60.0") -> None:
    """Every entity the scheduler reads, in a normal dry-night state."""
    b = data["bindings"]
    for z in data["zones"]:
        world.rachio.zone_switches.add(z["rachio_switch"])
        world.set(z["dominant_sensor"], dominant)
        world.set(z["state_sensor"], "Dry")
        for q in z["quality_sensors"]:
            world.set(q, "Good")
        world.set(f"switch.geodrops_rachio_{z['key']}_exclude", "off")
    world.set(b["drought_level_select"], level)
    world.set(b["standby_boolean"], "off")
    world.set(b["standby_switch"], "off")
    world.set(b["dew_formed_boolean"], "off")
    world.set(b["run_active_boolean"], "off")
    # Weather (brain defaults: Tempest entity ids).
    world.set("sensor.tempest_sensor_temperature", "60")
    world.set("sensor.tempest_sensor_humidity", "70")
    world.set("sensor.tempest_sensor_wind_speed_average", "5")
    world.set("sensor.tempest_rain_last_hour", "0")
    world.set("sensor.tempest_sensor_precipitation_type", "none")
    world.set("sensor.tempest_precipitation_today", "0")
    world.set("sensor.sun_next_dawn", "2026-07-02T04:30:00+00:00")
    world.set("sensor.sun_next_rising", "2026-07-02T05:00:00+00:00")
    for kind in ("forecast", "observed"):
        world.set(f"sensor.{kind}_overnight_temp", "60")
        world.set(f"sensor.{kind}_overnight_humidity", "80")
        world.set(f"sensor.{kind}_overnight_wind", "3")
    for h in (12, 18, 24):
        world.set(f"sensor.precipitation_chance_{h}_hour", "10")
        world.set(f"sensor.precipitation_amount_{h}_hour", "0")
```

- [ ] **Step 3: Write the harness** — `tests/engine/legacy_harness.py`:

```python
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
    native engine uses, so dataclasses from either side compare equal."""
    pkg = importlib.import_module("custom_components.geodrops_rachio.brain")
    sys.modules.setdefault("geodrops_rachio_lib", pkg)
    for name in _BRAIN_MODULES:
        mod = importlib.import_module(f"custom_components.geodrops_rachio.brain.{name}")
        sys.modules.setdefault(f"geodrops_rachio_lib.{name}", mod)


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
```

Note: `legacy_harness` imports `STATUS_ENTITY` from `engine.base`, which Task 6 creates. For this task, create `CC/engine/base.py` with only:

```python
"""Engine base: the former pyscript app's module globals as instance state."""
from __future__ import annotations

STATUS_ENTITY = "sensor.geodrops_rachio_status"
LOGBOOK_NAME = "Irrigation"
```

(Task 6 extends this file.)

- [ ] **Step 4: Write the smoke tests** — `tests/engine/test_legacy_harness.py`:

```python
from custom_components.geodrops_rachio.config_writer import build_config
from tests.engine.legacy_harness import load_legacy
from tests.engine.scenario import T_PLAN, entry_data, populate
from tests.engine.world import FakeWorld


def _legacy(freezer, data=None):
    freezer.move_to(T_PLAN)
    data = data or entry_data()
    world = FakeWorld(freezer)
    populate(world, data)
    return world, load_legacy(world, build_config(data))


def test_legacy_nightly_waters_both_zones(freezer):
    world, ns = _legacy(freezer)
    ns["irrigation_nightly"]()
    starts = [c for c in world.calls if c[:2] == ("rachio", "start_multiple_zone_schedule")]
    assert len(starts) == 1
    value, attrs = world.published["pyscript.geodrops_rachio_last_nightly"]
    assert attrs["aborted_reason"] is None
    assert sorted(attrs["watered"]) == ["back", "front"]
    assert world.published["pyscript.geodrops_rachio_status"][0] == "idle"
    assert world.get("switch.geodrops_rachio_run_active") == "off"


def test_legacy_preview_publishes_plan_without_watering(freezer):
    world, ns = _legacy(freezer)
    ns["_preview"]()
    assert not [c for c in world.calls if c[0] == "rachio"]
    value, attrs = world.published["pyscript.geodrops_rachio_preview"]
    assert value == 2 and set(attrs["planned_minutes"]) == {"front", "back"}


def test_legacy_standby_skips(freezer):
    world, ns = _legacy(freezer)
    world.set("switch.geodrops_rachio_standby", "on")
    ns["irrigation_nightly"]()
    assert world.published["pyscript.geodrops_rachio_status"][0] == "standby"
    assert not [c for c in world.calls if c[0] == "rachio"]
```

- [ ] **Step 5: Run**

Run: `bash tools/test.sh tests/engine/test_legacy_harness.py -q`
Expected: PASS (3). If the nightly test fails, the fault is in `world.py`/`scenario.py`/the harness, never the legacy file: inspect `world.calls`, `world.logs` and `world.published` to see where the legacy run went (a `never-started` abort usually means `RachioSim` did not turn a switch on).

- [ ] **Step 6: Commit**

```bash
git add tests/legacy tests/engine custom_components/geodrops_rachio/engine/base.py
git commit -m "test(engine): run the legacy pyscript app as a differential oracle

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `EngineStore`, legacy import, pyscript retirement

**Files:**
- Create: `CC/engine/store.py`
- Test: `tests/engine/test_store.py`

**Interfaces:**
- Produces:
  - Keys: `EFFICACY = "efficacy"`, `PENDING_OBS = "pending_obs"`, `WAITING_MARKER = "waiting_marker"`, `record_key(name) -> "record.<name>"`, `PERSISTED_RECORDS = ("last_nightly", "calibration", "targets", "preview")`, `LEGACY_FILE_KEYS: dict[basename, key]`.
  - `class EngineStore(docs: dict, save: Callable[[dict], Awaitable[None]])`: `read(key) -> Any | None` (deep copy), `async write(key, value)`, `async delete(key)`, `on_write: Callable[[], None] | None`.
  - `read_legacy_state(state_dir: Path) -> dict`, `remove_delivered(pyscript_dir: Path) -> list[str]`, `async async_open_store(hass, entry_id: str) -> EngineStore`.

- [ ] **Step 1: Write the failing tests** — `tests/engine/test_store.py`:

```python
import json

from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.geodrops_rachio.engine import store as es


async def _nosave(_docs):
    return None


async def test_engine_store_reads_are_copies_and_writes_save():
    saved = []

    async def save(docs):
        saved.append(docs)

    s = es.EngineStore({"efficacy": {"a": {"state": "calibrating"}}}, save)
    got = s.read(es.EFFICACY)
    got["a"]["state"] = "mutated"
    assert s.read(es.EFFICACY)["a"]["state"] == "calibrating"
    assert s.read(es.PENDING_OBS) is None
    await s.write(es.PENDING_OBS, [1])
    assert saved[-1]["pending_obs"] == [1]
    await s.delete(es.PENDING_OBS)
    assert s.read(es.PENDING_OBS) is None


async def test_on_write_callback_fires():
    s = es.EngineStore({}, _nosave)
    hits = []
    s.on_write = lambda: hits.append(1)
    await s.write(es.EFFICACY, {})
    assert hits == [1]


def _seed_legacy(config_dir):
    ps = config_dir / "pyscript"
    (ps / "modules" / "geodrops_rachio_lib").mkdir(parents=True)
    (ps / "modules" / "geodrops_rachio_lib" / "plan.py").write_text("x=1")
    (ps / "geodrops_rachio.py").write_text("# legacy")
    (ps / "geodrops_rachio_config.yaml").write_text("a: 1")
    (ps / ".geodrops_rachio_version").write_text("abc")
    state = ps / "geodrops_rachio_state"
    state.mkdir()
    (state / "irrigation_efficacy.json").write_text(json.dumps({"front": {"efficacy": 0.4}}))
    (state / "geodrops_rachio_last_nightly.json").write_text(
        json.dumps({"value": 2, "attributes": {"watered": ["front"]}}))
    return ps


async def test_open_store_imports_legacy_and_retires_pyscript(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)
    reloads = async_mock_service(hass, "pyscript", "reload")

    s = await es.async_open_store(hass, "entry1")

    assert s.read(es.EFFICACY) == {"front": {"efficacy": 0.4}}
    assert s.read(es.record_key("last_nightly"))["value"] == 2
    assert not (ps / "geodrops_rachio.py").exists()
    assert not (ps / "geodrops_rachio_config.yaml").exists()
    assert not (ps / ".geodrops_rachio_version").exists()
    assert not (ps / "modules" / "geodrops_rachio_lib").exists()
    assert (ps / "geodrops_rachio_state" / "irrigation_efficacy.json").exists()
    assert len(reloads) == 1
    stored = await Store(hass, es.STORAGE_VERSION, "geodrops_rachio.entry1").async_load()
    assert stored["docs"]["efficacy"] == {"front": {"efficacy": 0.4}}


async def test_open_store_second_time_keeps_store(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)
    async_mock_service(hass, "pyscript", "reload")
    s1 = await es.async_open_store(hass, "entry1")
    await s1.write(es.EFFICACY, {"front": {"efficacy": 0.9}})
    # Legacy file changes, but nothing was re-delivered: must NOT re-import.
    (ps / "geodrops_rachio_state" / "irrigation_efficacy.json").write_text("{}")
    reloads = async_mock_service(hass, "pyscript", "reload")
    s2 = await es.async_open_store(hass, "entry1")
    assert s2.read(es.EFFICACY) == {"front": {"efficacy": 0.9}}
    assert reloads == []


async def test_open_store_reimports_after_rollback_roundtrip(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)
    async_mock_service(hass, "pyscript", "reload")
    await es.async_open_store(hass, "entry1")
    # Rollback to v0.9.15 re-delivers the script and pyscript learns more.
    (ps / "geodrops_rachio.py").write_text("# legacy again")
    (ps / "geodrops_rachio_state" / "irrigation_efficacy.json").write_text(
        json.dumps({"front": {"efficacy": 0.7}}))
    s = await es.async_open_store(hass, "entry1")
    assert s.read(es.EFFICACY) == {"front": {"efficacy": 0.7}}


async def test_open_store_fresh_install_without_pyscript(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    s = await es.async_open_store(hass, "entry1")   # no pyscript dir, no service
    assert s.read(es.EFFICACY) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'store'`.

- [ ] **Step 3: Implement** — `CC/engine/store.py`:

```python
"""Engine persistence: named JSON documents in one HA Store per config entry.

The pyscript app kept its restart-surviving state as JSON files in
/config/pyscript/geodrops_rachio_state/. Each file becomes one document here,
so the ported code reads/writes exactly what it used to (see LEGACY_FILE_KEYS).
Reads return deep copies, preserving the app's read-fresh-from-file semantics.

Setup also retires the pyscript delivery: it deletes the files v0.9.x copied
into /config/pyscript/ and, if it deleted any, reloads pyscript so a legacy
script already loaded this boot cannot also water tonight.
"""
from __future__ import annotations

import copy
import json
import logging
import pathlib
import shutil
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
EFFICACY = "efficacy"
PENDING_OBS = "pending_obs"
WAITING_MARKER = "waiting_marker"
PERSISTED_RECORDS = ("last_nightly", "calibration", "targets", "preview")
LEGACY_STATE_DIRNAME = "geodrops_rachio_state"
DELIVERED_PATHS = (
    "geodrops_rachio.py",
    "geodrops_rachio_config.yaml",
    ".geodrops_rachio_version",
    "modules/geodrops_rachio_lib",
)


def record_key(name: str) -> str:
    return f"record.{name}"


LEGACY_FILE_KEYS = {
    "irrigation_efficacy.json": EFFICACY,
    "irrigation_pending_obs.json": PENDING_OBS,
    "irrigation_waiting.json": WAITING_MARKER,
    **{f"geodrops_rachio_{n}.json": record_key(n) for n in PERSISTED_RECORDS},
}


class EngineStore:
    def __init__(self, docs: dict, save: Callable[[dict], Awaitable[None]]) -> None:
        self._docs = copy.deepcopy(dict(docs or {}))
        self._save = save
        self.on_write: Callable[[], None] | None = None

    def read(self, key: str) -> Any | None:
        return copy.deepcopy(self._docs.get(key))

    async def write(self, key: str, value: Any) -> None:
        self._docs[key] = copy.deepcopy(value)
        await self._flush()

    async def delete(self, key: str) -> None:
        if self._docs.pop(key, None) is not None:
            await self._flush()

    async def _flush(self) -> None:
        await self._save(copy.deepcopy(self._docs))
        if self.on_write is not None:
            self.on_write()


def read_legacy_state(state_dir: pathlib.Path) -> dict:
    docs: dict = {}
    for fname, key in LEGACY_FILE_KEYS.items():
        path = state_dir / fname
        try:
            docs[key] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as err:
            _LOGGER.warning("geodrops_rachio: could not import legacy %s (%s)", fname, err)
    return docs


def remove_delivered(pyscript_dir: pathlib.Path) -> list[str]:
    removed: list[str] = []
    for rel in DELIVERED_PATHS:
        path = pyscript_dir / rel
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(rel)
        elif path.exists():
            path.unlink()
            removed.append(rel)
    return removed


async def async_open_store(hass: HomeAssistant, entry_id: str) -> EngineStore:
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
    data = await store.async_load()
    pyscript_dir = pathlib.Path(hass.config.path("pyscript"))
    removed = await hass.async_add_executor_job(remove_delivered, pyscript_dir)
    reloaded = False
    if removed and hass.services.has_service("pyscript", "reload"):
        await hass.services.async_call("pyscript", "reload", blocking=True)
        reloaded = True
    if data is None or removed:
        docs = await hass.async_add_executor_job(
            read_legacy_state, pyscript_dir / LEGACY_STATE_DIRNAME)
        data = {"docs": docs}
        await store.async_save(data)
        if docs or removed:
            _LOGGER.warning(
                "geodrops_rachio: native engine took over — imported %d legacy "
                "document(s) (%d zone(s) of calibration history); removed %s; "
                "pyscript reloaded: %s",
                len(docs), len(docs.get(EFFICACY) or {}),
                ", ".join(removed) or "nothing", reloaded)

    async def _save(docs: dict) -> None:
        await store.async_save({"docs": docs})

    return EngineStore(data.get("docs", {}), _save)
```

- [ ] **Step 4: Run**

Run: `bash tools/test.sh tests/engine/test_store.py -q`
Expected: PASS (6).

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/engine/store.py tests/engine/test_store.py
git commit -m "feat(engine): Store-backed engine state with legacy import + pyscript retirement

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `EngineBase` + `IOMixin`

**Files:**
- Modify: `CC/engine/base.py` (extend the Task 4 stub)
- Create: `CC/engine/io.py`
- Modify: `tests/engine/scenario.py` (add `native_engine`)
- Test: `tests/engine/test_io.py`

**Interfaces:**
- Consumes: `HAPort` (Task 3), `EngineStore` + keys (Task 5), `brain.config`.
- Produces (`base.py`):
  - `class EngineBase(port, store, load_raw_config: Callable[[], dict], fetch_zone_data: Callable[[str], Awaitable[tuple[dict, dict, dict]]])`
  - Attributes: `port, store, records: dict[str, dict]` (`{"value", "attributes"}`), `record_history: list[tuple[str, Any, dict]]`, and every former global (`_manual_stop, api_calls, state_polls, _runtime_cache, _current_tun, _current_cfg, _current_bindings, _run_in_progress, _watering_active, _rain_since`).
  - Methods: `_load_cfg() -> Config`, `_naive_now() -> datetime`, `add_listener(cb) -> Callable[[], None]`, `_notify_listeners()`, `_publish(name, value, attributes)`, `async _publish_record(name, value, attributes)`, `_restore_records()`, `async _activity(message, entity_id=None)`, `async _notify(message, title)`, `_set_status(status, detail=None)`.
- Produces (`io.py`): `class IOMixin` with `reset_counters, poll_zone_running, any_zone_running` (sync), `start_block, stop_zone, pause_device, resume_device, stop_device, set_run_active, stop_all` (async), `_fetch_zone_data, _refresh_zone_cache, get_runtimes, get_refill_depths, get_refill_spans` (async), `_read_efficacy_store` (sync), `_write_efficacy_store` (async), `_rained_since_run` (sync), `_append_pending_obs` (async); constants `RUNTIME_CACHE_TTL_S = 6 * 3600`.
- Produces (`scenario.py`): `native_engine(world, data, *mixins, docs=None) -> engine` composing `type("TestEngine", (*mixins, EngineBase), {})`.

- [ ] **Step 1: Write the failing tests** — `tests/engine/test_io.py`:

```python
import copy

import pytest

from custom_components.geodrops_rachio.engine import store as es
from custom_components.geodrops_rachio.engine.io import IOMixin
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld


def _eng(freezer, **kw):
    freezer.move_to(T_PLAN)
    world = FakeWorld(freezer)
    data = entry_data()
    populate(world, data)
    eng = native_engine(world, data, IOMixin, **kw)
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings
    eng._current_tun = eng._current_cfg.tunables
    return world, eng


async def test_start_block_hands_rachio_one_schedule(freezer):
    from custom_components.geodrops_rachio.brain.blocks import ZoneRun
    world, eng = _eng(freezer)
    runs = [ZoneRun("front", 12), ZoneRun("back", 10), ZoneRun("front", 5)]
    await eng.start_block(runs, {"front": "switch.front_zone", "back": "switch.back_zone"})
    assert world.calls[-1] == ("rachio", "start_multiple_zone_schedule", {
        "entity_id": ["switch.front_zone", "switch.back_zone", "switch.front_zone"],
        "duration": "12,10,5"})
    assert eng.api_calls == 1


async def test_pause_clamps_and_stop_device_swallows(freezer):
    world, eng = _eng(freezer)
    await eng.pause_device(500)
    assert world.calls[-1] == ("rachio", "pause_watering",
                               {"devices": "Main House", "duration": 60})
    await eng.pause_device(0)
    assert world.calls[-1][2]["duration"] == 1
    world.failing.add(("rachio", "stop_watering"))
    await eng.stop_device()          # must not raise
    assert eng.api_calls == 3


async def test_poll_counts_state_polls_not_api_calls(freezer):
    world, eng = _eng(freezer)
    assert eng.any_zone_running(["switch.front_zone", "switch.back_zone"]) is False
    assert eng.state_polls == 2 and eng.api_calls == 0


async def test_runtime_cache_ttl(freezer):
    world, eng = _eng(freezer)
    world.rachio_api = ({"id-front": 33.0}, {"id-front": 8.0}, {"id-front": 20.0})
    calls = []
    orig = eng._fetch_zone_data_fn

    async def counting(key):
        calls.append(key)
        return await orig(key)

    eng._fetch_zone_data_fn = counting
    assert await eng.get_runtimes() == {"id-front": 33.0}
    assert await eng.get_refill_depths() == {"id-front": 8.0}
    assert calls == ["rachio_api_key"]            # cached
    freezer.tick(6 * 3600 + 1)
    await eng.get_refill_spans()
    assert len(calls) == 2                        # TTL expired
    world.rachio_api = None
    assert await eng.get_runtimes(force=True) == {}   # fetch failure -> {}


async def test_efficacy_and_pending_obs_roundtrip(freezer):
    world, eng = _eng(freezer)
    assert eng._read_efficacy_store() == {}
    await eng._write_efficacy_store({"front": {"state": "calibrating"}})
    assert eng.store.read(es.EFFICACY) == {"front": {"state": "calibrating"}}
    await eng._append_pending_obs([{"zone": "front"}])
    await eng._append_pending_obs([{"zone": "back"}])
    assert [r["zone"] for r in eng.store.read(es.PENDING_OBS)] == ["front", "back"]


async def test_status_activity_notify(freezer):
    world, eng = _eng(freezer)
    seen = []
    eng.add_listener(lambda: seen.append(1))
    eng._set_status("waiting", detail="watering starts 03:10")
    assert eng.records["status"]["value"] == "waiting"
    assert eng.records["status"]["attributes"]["detail"] == "watering starts 03:10"
    assert seen == [1]
    await eng._activity("hello")
    assert world.calls[-1] == ("logbook", "log", {
        "name": "Irrigation", "message": "hello",
        "entity_id": "sensor.geodrops_rachio_status"})
    world.failing.add(("notify", "phone"))
    await eng._notify("msg", "Title")            # must not raise
    assert world.calls[-1] == ("notify", "phone", {"message": "msg", "title": "Title"})


async def test_publish_record_persists_and_restores(freezer):
    world, eng = _eng(freezer)
    await eng._publish_record("targets", 2, {"target_floors": {"front": 65.0}})
    assert eng.store.read(es.record_key("targets")) == {
        "value": 2, "attributes": {"target_floors": {"front": 65.0}}}
    world2, eng2 = _eng(freezer, docs=copy.deepcopy(eng.store._docs))
    assert eng2.records["targets"]["value"] == 2      # preloaded at construction
```

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_io.py -q`
Expected: FAIL — `ImportError` (`IOMixin` / `native_engine`).

- [ ] **Step 3: Add `native_engine` to `tests/engine/scenario.py`**:

```python
import copy

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.base import EngineBase
from custom_components.geodrops_rachio.engine.store import EngineStore
from tests.engine.world import FakePort


async def _nosave(_docs):
    return None


def fake_fetch(world):
    async def fetch(_key_name):
        if world.rachio_api is None:
            raise RuntimeError("rachio api down")
        return copy.deepcopy(world.rachio_api)
    return fetch


def native_engine(world, data, *mixins, docs=None):
    cls = type("TestEngine", (*mixins, EngineBase), {})
    return cls(FakePort(world), EngineStore(docs or {}, _nosave),
               lambda: build_config(data), fake_fetch(world))
```

- [ ] **Step 4: Implement `CC/engine/base.py`** (replace the stub):

```python
"""Engine base: the former pyscript app's module globals as instance state.

Ported from bundled_app/geodrops_rachio.py lines 121-213 and 1535-1571. The
app kept its run state in module globals because pyscript callbacks could not
close over locals; here it is plain instance state on one object.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Awaitable, Callable

from ..brain import config
from .port import HAPort
from .store import PERSISTED_RECORDS, EngineStore, record_key

_LOGGER = logging.getLogger(__name__)

STATUS_ENTITY = "sensor.geodrops_rachio_status"
LOGBOOK_NAME = "Irrigation"


class EngineBase:
    def __init__(self, port: HAPort, store: EngineStore,
                 load_raw_config: Callable[[], dict],
                 fetch_zone_data: Callable[[str], Awaitable[tuple[dict, dict, dict]]]) -> None:
        self.port = port
        self.store = store
        self._load_raw_config = load_raw_config
        self._fetch_zone_data_fn = fetch_zone_data
        self._manual_stop = False
        self.api_calls = 0     # Rachio-affecting service calls (start/stop) — the real budget
        self.state_polls = 0   # local HA state reads (free); tracked separately
        self._runtime_cache = {"ts": 0.0, "runtimes": {}, "depths": {}, "spans": {}}
        self._current_tun = None
        self._current_cfg = None
        self._current_bindings = None
        self._run_in_progress = False
        self._watering_active = False
        self._rain_since = None
        self.records: dict[str, dict] = {}
        self.record_history: list[tuple[str, Any, dict]] = []
        self._listeners: list[Callable[[], None]] = []
        self.store.on_write = self._notify_listeners
        # Sensors get last night's records immediately, not 30 s after startup.
        for name in PERSISTED_RECORDS:
            doc = self.store.read(record_key(name))
            if doc:
                self.records[name] = doc

    # --- plumbing ------------------------------------------------------------
    def _load_cfg(self):
        return config.parse_config(self._load_raw_config())

    def _naive_now(self) -> dt.datetime:
        return self.port.now().replace(tzinfo=None)

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
        return _remove

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _publish(self, name: str, value, attributes: dict) -> None:
        self.records[name] = {"value": value, "attributes": attributes}
        self.record_history.append((name, value, attributes))
        self._notify_listeners()

    async def _publish_record(self, name, value, attributes):
        """Publish a diagnostic state AND persist it, so a restart cannot erase it.

        A persistence failure must never take down the run that produced the
        record, so it warns and continues — the in-memory state is still
        published either way.
        """
        self._publish(name, value, attributes)
        try:
            await self.store.write(record_key(name), {"value": value, "attributes": attributes})
        except Exception as err:
            _LOGGER.warning(f"irrigation: could not persist {name} ({err})")

    def _restore_records(self):
        """Re-publish persisted diagnostic states after a restart."""
        for name in PERSISTED_RECORDS:
            data = self.store.read(record_key(name))
            if not data:
                continue
            self._publish(name, data["value"], data["attributes"])

    # --- ported: legacy lines 146-212 ---------------------------------------
    # _activity, _notify, _set_status: port per the Port rules. _activity's body
    # becomes:
    #     target = entity_id if entity_id is not None else STATUS_ENTITY
    #     await self.port.call("logbook", "log", {
    #         "name": LOGBOOK_NAME, "message": message, "entity_id": target})
```

Then port `_activity` (legacy 146–165, async), `_notify` (168–188, async; `service.call("notify", X, message=..., title=...)` → `await self.port.call("notify", X, {"message": message, "title": title})`), `_set_status` (191–212, sync; `"updated": self._naive_now().isoformat(timespec="seconds")`; final line `self._publish("status", status, attributes)`) into `EngineBase` per the Port rules. Delete the placeholder comment block once ported.

Note `_publish_record`/`_restore_records` above use record names without the `geodrops_rachio_` prefix (legacy `_publish_record("geodrops_rachio_targets", …)` → `_publish_record("targets", …)`).

- [ ] **Step 5: Implement `CC/engine/io.py`**:

```python
"""Rachio zone I/O, the runtime cache and calibration-document access.

Ported from bundled_app/geodrops_rachio.py lines 215-520 (the app's
@pyscript_compile file/HTTP helpers at 325-396 are gone: persistence is the
EngineStore and the Rachio fetch is injected as `fetch_zone_data`).
"""
from __future__ import annotations

import logging

from ..brain import blocks
from .store import EFFICACY, PENDING_OBS

_LOGGER = logging.getLogger(__name__)

RUNTIME_CACHE_TTL_S = 6 * 3600


class IOMixin:
    async def _fetch_zone_data(self):
        """(runtimes_minutes, refill_depths_mm, refill_spans_pts), one pass."""
        # _current_bindings may be unset only if this is called before any config
        # load at all; fall back to the documented default rather than crash.
        key_name = (
            self._current_bindings.rachio_api_key_secret if self._current_bindings
            else "rachio_api_key"
        )
        return await self._fetch_zone_data_fn(key_name)
```

Then port into `IOMixin`, per the Port rules, keeping docstrings:

| Method | Legacy lines | Kind |
|---|---|---|
| `reset_counters` | 217–220 | sync |
| `poll_zone_running` | 223–234 | sync |
| `any_zone_running` | 237–239 | sync |
| `start_block` | 242–264 | async |
| `stop_zone` | 267–270 | async |
| `pause_device` | 273–286 | async |
| `resume_device` | 289–293 | async |
| `stop_device` | 296–308 | async (worked example above) |
| `set_run_active` | 311–314 | async |
| `stop_all` | 317–322 | async |
| `_refresh_zone_cache` | 438–460 | async (`time.time()` → `self.port.now().timestamp()`; `_fetch_zone_data()` → `await self._fetch_zone_data()`) |
| `get_runtimes`, `get_refill_depths`, `get_refill_spans` | 463–481 | async |
| `_read_efficacy_store` | 487–490 | sync (`data = self.store.read(EFFICACY)`) |
| `_write_efficacy_store` | 493–494 | async |
| `_rained_since_run` | 497–507 | sync (`float(self.port.state(...))` still raises TypeError on None → same `except (TypeError, ValueError)`) |
| `_append_pending_obs` | 513–519 | async |

- [ ] **Step 6: Run**

Run: `bash tools/test.sh tests/engine -q`
Expected: PASS (all engine tests so far).

- [ ] **Step 7: Commit**

```bash
git add custom_components/geodrops_rachio/engine tests/engine
git commit -m "feat(engine): EngineBase + Rachio zone I/O (ported from pyscript app)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `RunnerMixin` + differential harness

**Files:**
- Create: `CC/engine/runner.py`
- Create: `tests/engine/diff.py`
- Test: `tests/engine/test_runner_diff.py`

**Interfaces:**
- Consumes: `IOMixin`, `EngineBase` (Task 6); harness (Task 4).
- Produces (`runner.py`): constants `CHECK_INTERVAL_S=30, EXTERNAL_STOP_POLLS=6, BLOCK_DRAIN_TIMEOUT_S=120, BLOCK_END_GRACE_S=90, BLOCK_START_CONFIRM_S=90, RESUME_PROBE_S=90` (with their legacy comments, lines 70–113); `class RunnerMixin` with `_abort_now` (sync), `_crumb` (sync), and async `run_plan(slots, zone_switches, is_standby, is_manual_stop, is_rain, is_rain_at_start) -> dict`, `run_collapsed(same) -> dict`, `_resume_took_hold(all_switches) -> bool`, `_walk_segment(steps, all_switches, is_standby, is_manual_stop, is_rain, crumbs) -> tuple`, `_await_block_end(zone_switches)`, `_sleep_watching(seconds, is_standby, is_manual_stop, is_rain, watch_switches=None, paused=False) -> tuple`.
- Produces (`diff.py`): `prime(target, cfg)`, `legacy_calls(world) -> list`, `assert_same_effects(legacy_world, legacy_files, native_world, native_engine)`, `status_trail_legacy(world)`, `status_trail_native(engine)`.

- [ ] **Step 1: Write `tests/engine/diff.py`**:

```python
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
```

- [ ] **Step 2: Write the failing differential tests** — `tests/engine/test_runner_diff.py`:

```python
import copy
import datetime as dt

import pytest

from custom_components.geodrops_rachio.brain import plan as brain_plan
from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from tests.engine.diff import legacy_calls, prime
from tests.engine.legacy_harness import load_legacy
from tests.engine.scenario import entry_data, native_engine, populate
from tests.engine.world import FakeWorld

T0 = "2026-07-02 02:00:00"
SWITCHES = {"front": "switch.front_zone", "back": "switch.back_zone"}


def _slots(cfg):
    tun = cfg.tunables
    p = brain_plan.build_plan(
        ["front", "back"], {"front": 30.0, "back": 24.0},
        {"front": "front", "back": "back"}, {"front": (), "back": ()}, 360.0, tun)
    return p.slots


def _setup_world(freezer, data, tweak):
    freezer.move_to(T0)
    world = FakeWorld(freezer)
    populate(world, data)
    flags = {"stop": False, "rain": False}
    tweak(world, flags)
    preds = (lambda: False, lambda: flags["stop"], lambda: flags["rain"], lambda: False)
    return world, flags, preds


SCENARIOS = {
    "normal": lambda w, f: None,
    "rachio_drop_recovers": lambda w, f: setattr(
        w.rachio, "drop_at", w.now() + dt.timedelta(minutes=20)),
    "never_started": lambda w, f: setattr(w.rachio, "refuse_next_start", True),
    "external_stop": lambda w, f: w.at("2026-07-02 02:10:00", w.rachio._clear),
    "manual_stop": lambda w, f: w.at("2026-07-02 02:10:00",
                                     lambda: f.__setitem__("stop", True)),
    "rain_abort": lambda w, f: w.at("2026-07-02 02:10:00",
                                    lambda: f.__setitem__("rain", True)),
}


@pytest.mark.parametrize("collapse", [True, False], ids=["collapsed", "run_plan"])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_runner_matches_legacy(freezer, name, collapse):
    overrides = "" if collapse else "use_pause_collapse: false"
    data = entry_data(overrides=overrides)
    runner = "run_collapsed" if collapse else "run_plan"

    lw, _lf, lpreds = _setup_world(freezer, data, SCENARIOS[name])
    ns = load_legacy(lw, build_config(data))
    cfg = ns["config"].parse_config(build_config(data))
    prime(ns, cfg)
    legacy_out = ns[runner](_slots(cfg), dict(SWITCHES), *lpreds)

    nw, _nf, npreds = _setup_world(freezer, data, SCENARIOS[name])
    eng = native_engine(nw, data, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    native_out = await getattr(eng, runner)(_slots(eng._current_cfg), dict(SWITCHES), *npreds)

    assert native_out == legacy_out
    assert nw.calls == legacy_calls(lw)
    assert (eng.api_calls, eng.state_polls) == (ns["api_calls"], ns["state_polls"])
```

- [ ] **Step 3: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_runner_diff.py -q`
Expected: FAIL — `ImportError: cannot import name 'RunnerMixin'`.

- [ ] **Step 4: Implement `CC/engine/runner.py`** — header:

```python
"""Plan execution: hand Rachio its schedule, then watch it (poll-verify + aborts).

Ported from bundled_app/geodrops_rachio.py lines 70-113 (constants) and
522-938. Every sleep goes through the HAPort, so a test clock drives it.
"""
from __future__ import annotations

import logging

from ..brain import abort, blocks, program, recovery

_LOGGER = logging.getLogger(__name__)

# (copy constants CHECK_INTERVAL_S .. RESUME_PROBE_S with their comments, legacy 70-113)


class RunnerMixin:
    ...
```

Port per the Port rules:

| Method | Legacy lines | Kind | Notes |
|---|---|---|---|
| `_abort_now` | 524–525 | sync | |
| `run_plan` | 528–642 | async | `log.warning` → `_LOGGER.warning` |
| `_crumb` | 645–656 | sync | `dt.datetime.now().astimezone()` → `self.port.now()` |
| `run_collapsed` | 659–796 | async | `global api_calls` → `self.api_calls`; inline `service.call("rachio", "start_multiple_zone_schedule", …)` → `await self.port.call(...)`; `_current_cfg.tunables` → `self._current_cfg.tunables` |
| `_resume_took_hold` | 799–814 | async | |
| `_walk_segment` | 817–866 | async | |
| `_await_block_end` | 869–889 | async | |
| `_sleep_watching` | 892–938 | async | |

Keep both `except Exception:` safety nets and the `finally: set_run_active(False)` exactly — the unload safety stop is added in the scheduler (Task 11), not here.

- [ ] **Step 5: Run**

Run: `bash tools/test.sh tests/engine/test_runner_diff.py -q`
Expected: PASS (12 = 6 scenarios × 2 runners). On failure, diff `nw.calls` against `legacy_calls(lw)` to find the first divergent call; the port is wrong there.

- [ ] **Step 6: Commit**

```bash
git add custom_components/geodrops_rachio/engine/runner.py tests/engine
git commit -m "feat(engine): valve runners, proven equal to legacy by differential tests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: `PlanningMixin`

**Files:**
- Create: `CC/engine/planning.py`
- Test: `tests/engine/test_planning_diff.py`

**Interfaces:**
- Consumes: Tasks 6–7.
- Produces: `SENSOR_SKIP_REASONS = frozenset(("unavailable", "low_quality"))`; `class PlanningMixin` with sync `_zone_excluded(zone)`, `_is_standby()`, `_is_manual_stop()`, `_is_rain()`, `_rain_condition_now()`, `_is_rain_at_start()`, `_read_weather(tun)`, `_forecast_num(entity)`, `_read_forecast_weather()`, `_read_observed_overnight()`, `_read_forecast_precip(horizon_hours)`, `_read_zone_signals(zone)`, `_sensor_last_updated(entity)`, `_dawn_time()`, `_end_anchor_time(anchor)`, `_resolve_profile(cfg)`, `_compute_target_floors(cfg)`, `_pressure_pair(ctx)`, `_rain_skip_check(ctx)`; async `_publish_targets(cfg)`, `_plan_context(cfg) -> dict`.

- [ ] **Step 1: Write the failing differential tests** — `tests/engine/test_planning_diff.py`:

```python
import random

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin
from tests.engine.diff import legacy_calls, prime
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


EFFICACY_CAL = {"front": {"state": "calibrating"}, "back": {"state": "converged",
                "efficacy": 0.5, "span_pts": 12.0}}

SCENARIOS = {
    "normal": (False, {}, lambda w: None),
    "excluded": (False, {}, lambda w: w.set("switch.geodrops_rachio_back_exclude", "on")),
    "low_quality": (False, {}, lambda w: w.set("sensor.front_q1", "Bad") or w.set("sensor.front_q2", "Bad")),
    "forecast_missing": (False, {}, lambda w: w.remove("sensor.forecast_overnight_temp")),
    "one_zone_wet": (False, {}, lambda w: w.set("sensor.back_dominant", "80.0")),
    "probe_calibrating": (True, {"irrigation_efficacy.json": EFFICACY_CAL},
                          lambda w: w.set("sensor.front_dominant", "70.0")),
    "live_runtimes": (False, {}, lambda w: setattr(w, "rachio_api", (
        {"id-front": 33.0}, {"id-front": 8.0}, {"id-front": 20.0}))),
    "level_4_emergency": (False, {}, lambda w: w.set(
        "select.geodrops_rachio_drought_level", "Level 4 - Emergency")),
}


def _world(freezer, data, tweak):
    freezer.move_to(T_PLAN)
    w = FakeWorld(freezer)
    populate(w, data)
    tweak(w)
    return w


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_plan_context_matches_legacy(freezer, name):
    self_cal, files, tweak = SCENARIOS[name]
    data = entry_data(self_cal=self_cal)

    lw = _world(freezer, data, tweak)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    cfg = ns["config"].parse_config(build_config(data))
    prime(ns, cfg)
    legacy_ctx = ns["_plan_context"](cfg)
    ns["_publish_targets"](cfg)
    legacy_skip = ns["_rain_skip_check"](legacy_ctx)

    nw = _world(freezer, data, tweak)
    docs = {LEGACY_FILE_KEYS[f]: v for f, v in files.items()}
    eng = native_engine(nw, data, PlanningMixin, RunnerMixin, IOMixin, docs=docs)
    ncfg = eng._load_cfg()
    prime(eng, ncfg)
    native_ctx = await eng._plan_context(ncfg)
    await eng._publish_targets(ncfg)
    native_skip = eng._rain_skip_check(native_ctx)

    assert native_ctx == legacy_ctx
    assert native_skip == legacy_skip
    assert nw.calls == legacy_calls(lw)
    assert eng.records["targets"]["attributes"] == \
        lw.published["pyscript.geodrops_rachio_targets"][1]
    assert eng.store.read("efficacy") == lf.files.get("irrigation_efficacy.json")


async def test_is_rain_sustain_and_hail(freezer):
    data = entry_data()
    w = _world(freezer, data, lambda w: None)
    eng = native_engine(w, data, PlanningMixin, RunnerMixin, IOMixin)
    prime(eng, eng._load_cfg())
    w.set("sensor.tempest_sensor_precipitation_type", "rain")
    w.set("sensor.tempest_rain_last_hour", "1.0")
    assert eng._is_rain() is False          # sustain clock starts
    assert eng._is_rain_at_start() is True  # no sustain at block start
    freezer.tick(151)
    assert eng._is_rain() is True
    w.set("sensor.tempest_sensor_precipitation_type", "hail")
    eng._rain_since = None
    assert eng._is_rain() is True           # hail is immediate
```

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_planning_diff.py -q`
Expected: FAIL — `ImportError: cannot import name 'PlanningMixin'`.

- [ ] **Step 3: Implement `CC/engine/planning.py`** — header:

```python
"""Planning: abort predicates, sensor/weather/sun reads, and the nightly plan.

Ported from bundled_app/geodrops_rachio.py lines 941-1532 and 1716-1752.
"""
from __future__ import annotations

import datetime as dt
import logging
import random

from ..brain import (
    calibration, config, dosing, drought, evaluate, plan, sensors, weather)

_LOGGER = logging.getLogger(__name__)

# Offline reasons that mean the SENSOR was unusable at plan time (as opposed to
# "excluded" or being above target). A calibrating zone skipped for one of these
# is a sensor-recovery probe candidate. Mirrors sensors.read_zone.
SENSOR_SKIP_REASONS = frozenset(("unavailable", "low_quality"))


class PlanningMixin:
    ...
```

Port per the Port rules (all sync unless marked):

| Method | Legacy lines | Notes |
|---|---|---|
| `_zone_excluded` | 943–963 | `except NameError` → `is None` check is implicit (`None == "on"` is False) |
| `_is_standby` | 966–987 | same |
| `_is_manual_stop` | 990–991 | |
| `_is_rain` | 994–1023 | `time.time()` → `self.port.now().timestamp()` |
| `_rain_condition_now` | 1026–1046 | |
| `_is_rain_at_start` | 1049–1067 | |
| `_read_weather` | 1070–1102 | inner `num`: `raw = self.port.state(entity)`; `if raw is None:` → the "not found" warning + default; else `try: float(raw) except (ValueError, TypeError): default`. inner `text`: `None` → "not found" warning + default |
| `_forecast_num` | 1105–1123 | `None` → "not found" warning + `None` |
| `_read_forecast_weather` | 1126–1150 | |
| `_read_observed_overnight` | 1153–1170 | |
| `_read_forecast_precip` | 1173–1180 | |
| `_read_zone_signals` | 1183–1190 | |
| `_sensor_last_updated` | 1193–1205 | `self.port.last_updated(entity)` inside the same `try/except Exception` |
| `_dawn_time` | 1208–1209 | |
| `_end_anchor_time` | 1212–1231 | `raw = self.port.state(entity)`; `if raw is None:` warning + `self._dawn_time()`; else `dt.datetime.fromisoformat(raw)` |
| `_resolve_profile` | 1234–1257 | `level = self.port.state(...)` (None already means missing) |
| `_compute_target_floors` | 1260–1277 | |
| `_publish_targets` | 1280–1290 | **async**; `_publish_record("targets", …)`; `updated` via `self._naive_now()` |
| `_plan_context` | 1293–1517 | **async**; `random.Random()` stays (the test seeds it); `get_runtimes()` etc. awaited; `_write_efficacy_store` awaited; `dt.datetime.now().astimezone()` → `self.port.now()`; `dt.datetime.now(end.tzinfo)` → `self.port.now().astimezone(end.tzinfo)` |
| `_pressure_pair` | 1520–1532 | |
| `_rain_skip_check` | 1716–1752 | |

- [ ] **Step 4: Run**

Run: `bash tools/test.sh tests/engine/test_planning_diff.py -q`
Expected: PASS (9). The `forecast_missing` scenario is where legacy raises-and-catches `NameError` while native sees `None`: both must reach `forecast_wx is None` with identical calls.

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/engine/planning.py tests/engine/test_planning_diff.py
git commit -m "feat(engine): planning + predicates, proven equal to legacy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: `OrchestrationMixin` (nightly run, preview, recap)

**Files:**
- Create: `CC/engine/orchestration.py`
- Test: `tests/engine/test_orchestration_diff.py`

**Interfaces:**
- Consumes: Tasks 6–8.
- Produces: `class OrchestrationMixin` with async `_write_waiting_marker(window_end_iso, stamp, trigger)`, `_clear_waiting_marker()`, `_publish_last_run(stamp, trigger, ctx=None, result=None, outcome=None, skipped=None)`, `_plan_and_run(wait, trigger)`, `_preview()`, `_preview_body(cfg, stamp)`, `_log_zone_outcomes(cfg, watered, delivered, uncompleted)`, `_report(result, tun, event_time=None, started_at=None, ended_at=None)`; sync `_read_waiting_marker()`.
- `tests/engine/scenario.py` gains `ALL_MIXINS = (OrchestrationMixin, PlanningMixin, RunnerMixin, IOMixin)` (append after the import is valid, i.e. in this task).

- [ ] **Step 1: Write the failing differential tests** — `tests/engine/test_orchestration_diff.py`:

```python
import random

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.diff import assert_same_effects
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import ALL_MIXINS, T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


CAL = {"irrigation_efficacy.json": {"front": {"state": "calibrating"},
                                    "back": {"state": "calibrating"}}}


def _set(entity, value):
    return lambda w, eng=None: w.set(entity, value)


# name -> (self_cal, seed files, [(time, action)], wait, trigger)
# action(world, target) where target is the legacy ns (dict) or the native engine.
SCENARIOS = {
    "normal_night": (False, {}, [], True, "nightly"),
    "standby": (False, {}, [("2026-07-01 22:59:59", _set("switch.geodrops_rachio_standby", "on"))], True, "nightly"),
    "rain_skip_at_plan": (False, {}, [
        ("2026-07-01 22:59:59", _set("sensor.precipitation_chance_18_hour", "90")),
        ("2026-07-01 22:59:59", _set("sensor.precipitation_amount_18_hour", "25"))], True, "nightly"),
    "rain_skip_at_window_start": (False, {}, [
        ("2026-07-02 01:00:00", _set("sensor.precipitation_chance_18_hour", "90")),
        ("2026-07-02 01:00:00", _set("sensor.precipitation_amount_18_hour", "25"))], True, "nightly"),
    "moisture_risen": (False, {}, [("2026-07-02 01:00:00", _set("sensor.front_dominant", "80.0"))], True, "nightly"),
    "all_moisture_risen": (False, {}, [
        ("2026-07-02 01:00:00", _set("sensor.front_dominant", "80.0")),
        ("2026-07-02 01:00:00", _set("sensor.back_dominant", "80.0"))], True, "nightly"),
    "sensor_recovery_probe": (True, CAL, [
        ("2026-07-01 22:59:59", _set("sensor.back_q1", "Bad")),
        ("2026-07-01 22:59:59", _set("sensor.back_q2", "Bad")),
        ("2026-07-02 00:10:00", _set("sensor.back_q1", "Good")),
        ("2026-07-02 00:10:00", _set("sensor.back_q2", "Good"))], True, "nightly"),
    "run_now": (False, {}, [], False, "run_now"),
    "hail_abort_mid_run": (False, {}, [("2026-07-02 04:10:00", _set(
        "sensor.tempest_sensor_precipitation_type", "hail"))], True, "nightly"),
    "notify_and_calendar_fail": (False, {}, [], True, "nightly"),
    "preview_during_wait": (False, {}, [("2026-07-02 01:00:00", "preview")], True, "nightly"),
}


def _schedule(world, events, target):
    for when, action in events:
        if action == "preview":
            fn = (lambda t=target: t["_preview"]()) if isinstance(target, dict) \
                else (lambda t=target: t._preview())
        else:
            fn = (lambda a=action: a(world))
        world.at(when, fn)


def _prepare(freezer, name, data):
    freezer.move_to("2026-07-01 22:59:58")
    w = FakeWorld(freezer)
    populate(w, data)
    if name == "notify_and_calendar_fail":
        w.failing |= {("notify", "phone"), ("calendar", "create_event")}
    return w


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_night_matches_legacy(freezer, name):
    self_cal, files, events, wait, trigger = SCENARIOS[name]
    data = entry_data(self_cal=self_cal)

    lw = _prepare(freezer, name, data)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    _schedule(lw, events, ns)
    lw.advance(2)                      # fire the 22:59:59 setup events; now 23:00:00
    ns["_plan_and_run"](wait, trigger)

    nw = _prepare(freezer, name, data)
    eng = native_engine(nw, data, *ALL_MIXINS,
                        docs={LEGACY_FILE_KEYS[f]: v for f, v in files.items()})
    _schedule(nw, events, eng)
    for coro in nw.advance(2):
        await coro
    await eng._plan_and_run(wait, trigger)

    assert_same_effects(lw, lf, nw, eng)


async def test_preview_refused_while_watering(freezer):
    data = entry_data()
    w = _prepare(freezer, "x", data)
    eng = native_engine(w, data, *ALL_MIXINS)
    eng._current_bindings = eng._load_cfg().bindings
    eng._watering_active = True
    await eng._preview()
    assert "preview" not in eng.records
    assert w.calls[-1][2]["message"] == "Preview skipped: watering in progress"
```

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_orchestration_diff.py -q`
Expected: FAIL — `ImportError` (`ALL_MIXINS`).

- [ ] **Step 3: Implement `CC/engine/orchestration.py`** — header:

```python
"""The nightly run end to end: plan, wait, re-check, water, record, recap.

Ported from bundled_app/geodrops_rachio.py lines 1574-1713 and 1755-2367.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..brain import (
    calibration, dosing, evaluate, plan, recovery, report_format, sensors)
from .store import WAITING_MARKER

_LOGGER = logging.getLogger(__name__)


class OrchestrationMixin:
    ...
```

Port per the Port rules:

| Method | Legacy lines | Kind | Notes |
|---|---|---|---|
| `_write_waiting_marker` | 1574–1585 | async | `await self.store.write(WAITING_MARKER, {...})` inside the same try |
| `_clear_waiting_marker` | 1588–1594 | async | `await self.store.delete(WAITING_MARKER)` |
| `_read_waiting_marker` | 1597–1603 | sync | `return self.store.read(WAITING_MARKER)` inside the same try |
| `_publish_last_run` | 1606–1713 | async | `state.set("pyscript.geodrops_rachio_last_run", …)` → `self._publish("last_run", value, attributes)`; `_publish_record("geodrops_rachio_last_nightly", …)` → `await self._publish_record("last_nightly", …)`; `api_calls`/`state_polls` → `self.` |
| `_plan_and_run` | 1755–2125 | async | predicates passed as `self._is_standby, self._is_manual_stop, self._is_rain, self._is_rain_at_start`; `dt.datetime.now(start.tzinfo)` → `self.port.now().astimezone(start.tzinfo)`; `dt.datetime.now().astimezone()` → `self.port.now()`; `stamp` via `self._naive_now()` |
| `_preview` | 2128–2162 | async | keep the save/restore of `_current_cfg`/`_current_bindings` |
| `_preview_body` | 2165–2297 | async | `_publish_record("preview", …)` |
| `_log_zone_outcomes` | 2300–2324 | async | |
| `_report` | 2327–2367 | async | `fallback = event_time if event_time is not None else self._naive_now()` |

Then append to `tests/engine/scenario.py`:

```python
from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.orchestration import OrchestrationMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from custom_components.geodrops_rachio.engine.runner import RunnerMixin

ALL_MIXINS = (OrchestrationMixin, PlanningMixin, RunnerMixin, IOMixin)
```

- [ ] **Step 4: Run**

Run: `bash tools/test.sh tests/engine/test_orchestration_diff.py -q`
Expected: PASS (12). Equivalence alone is not enough: confirm on the LEGACY side that each scenario exercises its branch — `last_run` attributes show `skipped == "standby" / "rain-forecast" / "rain-forecast-at-window-start" / "moisture-risen"`, `window_start_dropped` for `moisture_risen`, `recovery_added` for `sensor_recovery_probe`, `aborted_reason == "rain"` for `hail_abort_mid_run` (the event must land inside the watering span: compare with the plan's `start`), and a `pyscript.geodrops_rachio_preview` publish for `preview_during_wait`. If a scenario misses its branch, fix the scenario's numbers/times — never the port.

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/engine/orchestration.py tests/engine
git commit -m "feat(engine): nightly orchestration + preview, proven equal to legacy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `LearningMixin` (06:00 calibration, settle-and-learn)

**Files:**
- Create: `CC/engine/learning.py`
- Test: `tests/engine/test_learning_diff.py`

**Interfaces:**
- Consumes: Tasks 6–9.
- Produces: `class LearningMixin` with async `irrigation_calibrate()` and `_settle_and_learn()`. `ALL_MIXINS` in `scenario.py` becomes `(LearningMixin, OrchestrationMixin, PlanningMixin, RunnerMixin, IOMixin)`.

- [ ] **Step 1: Write the failing differential tests** — `tests/engine/test_learning_diff.py`:

```python
import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.diff import assert_same_effects
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import ALL_MIXINS, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

RUN_END = "2026-07-02T04:30:00+00:00"


def _obs(zone):
    return {"zone": zone, "pre_dominant": 60.0, "minutes": 20, "run_end_iso": RUN_END,
            "peak": None, "retained": None, "last_seen_updated": None}


SETTLE = {
    "accept": ({"sensor.front_dominant": [("2026-07-02 05:00:00", "68.0"),
                                          ("2026-07-02 07:00:00", "66.0")]}, {}),
    "no_rise_reject": ({"sensor.front_dominant": [("2026-07-02 05:00:00", "59.0")]}, {}),
    "training": ({"sensor.front_dominant": [("2026-07-02 05:00:00", "68.0")]},
                 {"sensor.front_q1": "Training", "sensor.front_q2": "Training",
                  "sensor.front_q3": "Training"}),
    "expired_no_fresh_reading": ({}, {}),
}


def _world(freezer, data, readings, statics):
    freezer.move_to("2026-07-02 04:30:00")
    w = FakeWorld(freezer)
    populate(w, data)
    for ent, val in statics.items():
        w.set(ent, val)
    for ent, series in readings.items():
        for when, val in series:
            w.at(when, lambda e=ent, v=val: w.set(e, v))
    return w


@pytest.mark.parametrize("name", list(SETTLE))
async def test_settle_matches_legacy(freezer, name):
    readings, statics = SETTLE[name]
    data = entry_data(self_cal=True)
    files = {"irrigation_pending_obs.json": [_obs("front")],
             "irrigation_efficacy.json": {"front": {"state": "calibrating"}}}

    lw = _world(freezer, data, readings, statics)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    for _ in range(80):                 # 40 h of 30-min polls
        lw.advance(1800)
        ns["_settle_and_learn"]()

    nw = _world(freezer, data, readings, statics)
    eng = native_engine(nw, data, *ALL_MIXINS,
                        docs={LEGACY_FILE_KEYS[f]: v for f, v in files.items()})
    for _ in range(80):
        for coro in nw.advance(1800):
            await coro
        await eng._settle_and_learn()

    assert_same_effects(lw, lf, nw, eng)


@pytest.mark.parametrize("has_nightly", [True, False])
async def test_calibrate_matches_legacy(freezer, has_nightly):
    data = entry_data()
    nightly_attrs = {"pressure_forecast": {"warm": False, "humid": True,
                                           "stagnant": False, "count": 1}}

    freezer.move_to("2026-07-02 06:00:00")
    lw = FakeWorld(freezer)
    populate(lw, data)
    lf = LegacyFiles()
    ns = load_legacy(lw, build_config(data), lf)
    if has_nightly:
        lw.publish("pyscript.geodrops_rachio_last_nightly", 2, nightly_attrs)
    ns["irrigation_calibrate"]()

    freezer.move_to("2026-07-02 06:00:00")
    nw = FakeWorld(freezer)
    populate(nw, data)
    eng = native_engine(nw, data, *ALL_MIXINS)
    if has_nightly:
        eng._publish("last_nightly", 2, nightly_attrs)
    await eng.irrigation_calibrate()

    assert_same_effects(lw, lf, nw, eng)
```

Note: `assert_same_effects` compares record histories, so the seeded `last_nightly` publish is present on both sides.

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_learning_diff.py -q`
Expected: FAIL — `ImportError: cannot import name 'LearningMixin'`.

- [ ] **Step 3: Implement `CC/engine/learning.py`**:

```python
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
    ...
```

Port per the Port rules:

| Method | Legacy lines | Notes |
|---|---|---|
| `irrigation_calibrate` | 2372–2458 | async. `global` lines dropped. `state.getattr("pyscript.geodrops_rachio_last_nightly").get("pressure_forecast")` + `except NameError` → `attrs = (self.records.get("last_nightly") or {}).get("attributes"); forecast_pb = attrs.get("pressure_forecast") if attrs is not None else None`. `_publish_record("calibration", …)`. The final `_activity(..., entity_id="pyscript.geodrops_rachio_calibration")` → `entity_id=STATUS_ENTITY` |
| `_settle_and_learn` | 2467–2601 | async. `task.executor(_read_json, PENDING_OBS_PATH)` → `self.store.read(PENDING_OBS)`; `task.executor(_write_json_atomic, PENDING_OBS_PATH, remaining)` → `await self.store.write(PENDING_OBS, remaining)`; `get_runtimes()` awaited |

Update `ALL_MIXINS` in `tests/engine/scenario.py` to prepend `LearningMixin` (import it).

- [ ] **Step 4: Run**

Run: `bash tools/test.sh tests/engine -q`
Expected: PASS (all engine tests, incl. 6 new). Equivalence alone is not enough:
confirm each settle scenario reaches its named outcome on the LEGACY side
(`lf.files["irrigation_efficacy.json"]["front"]` shows `n_obs == 1` for
`accept`, `last_reject_reason == "no_rise"` for `no_rise_reject`, `state` reset
for `training`, and an emptied pending queue with no model change for
`expired_no_fresh_reading`). If a scenario lands on the wrong branch, adjust its
readings/times (brain tunables `settle_hours`, `retain_hours`,
`settle_max_wait_hours` decide the windows) — never the port.

- [ ] **Step 5: Commit**

```bash
git add custom_components/geodrops_rachio/engine/learning.py tests/engine
git commit -m "feat(engine): calibration + settle-and-learn, proven equal to legacy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: `Scheduler` (lifecycle, startup recovery, actions, unload safety)

**Files:**
- Create: `CC/engine/scheduler.py`
- Modify: `tests/engine/scenario.py` (add `native_scheduler`)
- Test: `tests/engine/test_scheduler.py`

**Interfaces:**
- Consumes: Tasks 5–10.
- Produces: `class Scheduler(LearningMixin, OrchestrationMixin, PlanningMixin, RunnerMixin, IOMixin, EngineBase)`:
  - `__init__(port, store, load_raw_config, fetch_zone_data, create_task: Callable[[Coroutine, str], asyncio.Task])`
  - `run_task: asyncio.Task | None`, `startup_task: asyncio.Task | None`
  - `async _cancel_run()`, `async _start_run(wait: bool, trigger: str)`
  - Trigger bodies: `async irrigation_nightly()`, `async _on_startup()`
  - Actions: `async async_run_now()`, `async async_preview()`, `async request_stop()`, `async async_reset()`, `async async_refresh_runtimes()`
  - HA wiring: `async_start(hass) -> None` (registers triggers + startup), `async async_shutdown() -> None` (unsubscribe, cancel, unload safety stop)
- Produces (`scenario.py`): `native_scheduler(world, data, docs=None) -> Scheduler` using `asyncio.get_running_loop().create_task`.

- [ ] **Step 1: Write the failing tests** — `tests/engine/test_scheduler.py`:

```python
import asyncio
import random

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.diff import assert_same_effects
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


WAITING = {"irrigation_waiting.json": {
    "window_end": "2026-07-02T04:59:00+00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
MISSED = {"irrigation_waiting.json": {
    "window_end": "2026-07-02T01:00:00+00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}

# name -> (start time, seed files, world tweak)
STARTUP = {
    "daytime_noop": ("2026-07-02 14:00:00", {}, lambda w: None),
    "collapsed_marker": ("2026-07-02 02:00:00", {},
                         lambda w: w.set("switch.geodrops_rachio_run_active", "on")),
    "orphan_valve": ("2026-07-02 02:00:00", {},
                     lambda w: w.rachio.start_direct([("switch.front_zone", 30)])),
    "waiting_rearm": ("2026-07-02 00:30:00", WAITING, lambda w: None),
    "waiting_missed": ("2026-07-02 02:00:00", MISSED, lambda w: None),
}


@pytest.mark.parametrize("name", list(STARTUP))
async def test_startup_matches_legacy(freezer, name):
    start, files, tweak = STARTUP[name]
    data = entry_data()

    freezer.move_to(start)
    lw = FakeWorld(freezer)
    populate(lw, data)
    tweak(lw)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    ns["_on_startup"]()

    freezer.move_to(start)
    nw = FakeWorld(freezer)
    populate(nw, data)
    tweak(nw)
    eng = native_scheduler(nw, data, docs={LEGACY_FILE_KEYS[f]: v for f, v in files.items()})
    await eng._on_startup()
    if eng.run_task is not None:
        await eng.run_task

    assert_same_effects(lw, lf, nw, eng)


async def test_manual_stop_mid_run_matches_legacy(freezer):
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    lw = FakeWorld(freezer)
    populate(lw, data)
    lf = LegacyFiles()
    ns = load_legacy(lw, build_config(data), lf)
    lw.at("2026-07-02 04:20:00", lambda: ns["_on_stop_button"]("2026-07-02T04:20:00"))
    ns["irrigation_nightly"]()

    freezer.move_to("2026-07-01 23:00:00")
    nw = FakeWorld(freezer)
    populate(nw, data)
    eng = native_scheduler(nw, data)
    nw.at("2026-07-02 04:20:00", eng.request_stop)
    await eng.irrigation_nightly()
    await eng.run_task

    assert_same_effects(lw, lf, nw, eng)
    assert lw.published["pyscript.geodrops_rachio_last_run"][1]["aborted_reason"] == "manual-stop"


async def test_reset_cancels_waiting_run(freezer):
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    # Park the run in its pre-dawn wait: make sleep block until cancelled.
    gate = asyncio.Event()

    async def blocking_sleep(_s):
        await gate.wait()
    eng.port.sleep = blocking_sleep
    await eng.irrigation_nightly()
    for _ in range(5):                 # let the run task reach its wait
        await asyncio.sleep(0)
    assert eng.records["status"]["value"] == "waiting"
    await eng.async_reset()
    assert eng.run_task is None
    assert eng.records["status"]["value"] == "idle"
    assert ("rachio", "stop_watering", {"devices": "Main House"}) in w.calls
    assert eng.store.read("waiting_marker") is None


async def test_unload_mid_pause_stops_the_device(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 02:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    paused = asyncio.Event()
    real_sleep = eng.port.sleep

    async def sleep_until_paused(seconds):
        if w.rachio.paused_until is not None:
            paused.set()
            await asyncio.Event().wait()       # park here until cancelled
        await real_sleep(seconds)
    eng.port.sleep = sleep_until_paused
    await eng.async_run_now()
    await asyncio.wait_for(paused.wait(), 5)
    await eng.async_shutdown()
    assert eng.run_task is None
    tail = w.calls[-4:]
    assert ("rachio", "stop_watering", {"devices": "Main House"}) in tail
    assert ("switch", "turn_off", {"entity_id": "switch.front_zone"}) in tail
    assert w.get("switch.geodrops_rachio_run_active") == "off"


async def test_refresh_runtimes_publishes_record(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    w.rachio_api = ({"id-front": 33.0}, {"id-front": 8.0}, {})
    eng = native_scheduler(w, data)
    await eng.async_refresh_runtimes()
    rec = eng.records["runtimes"]
    assert rec["value"] == 1 and rec["attributes"]["refill_depths_mm"] == {"id-front": 8.0}
```

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/engine/test_scheduler.py -q`
Expected: FAIL — `ImportError: cannot import name 'native_scheduler'`.

- [ ] **Step 3: Add `native_scheduler`** to `tests/engine/scenario.py`:

```python
import asyncio

from custom_components.geodrops_rachio.engine.scheduler import Scheduler


def native_scheduler(world, data, docs=None):
    loop = asyncio.get_running_loop()
    return Scheduler(FakePort(world), EngineStore(docs or {}, _nosave),
                     lambda: build_config(data), fake_fetch(world),
                     lambda coro, name: loop.create_task(coro, name=name))
```

- [ ] **Step 4: Implement `CC/engine/scheduler.py`**:

```python
"""The Scheduler: every engine mixin composed, plus triggers and lifecycle.

Ported from bundled_app/geodrops_rachio.py lines 2461-2464 and 2604-2847
(triggers, startup recovery, services), with pyscript's task.unique replaced by
one owned run task.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from typing import Callable, Coroutine

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started

from ..brain import recovery
from .base import EngineBase
from .io import IOMixin
from .learning import LearningMixin
from .orchestration import OrchestrationMixin
from .planning import PlanningMixin
from .runner import RunnerMixin

_LOGGER = logging.getLogger(__name__)

SAFETY_STOP_TIMEOUT_S = 10


class Scheduler(LearningMixin, OrchestrationMixin, PlanningMixin, RunnerMixin,
                IOMixin, EngineBase):
    def __init__(self, port, store, load_raw_config, fetch_zone_data,
                 create_task: Callable[[Coroutine, str], asyncio.Task]) -> None:
        super().__init__(port, store, load_raw_config, fetch_zone_data)
        self._create_task = create_task
        self.run_task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self._unsubs: list[Callable[[], None]] = []

    # --- the one run task (replaces task.unique("geodrops_rachio_run")) -------
    async def _cancel_run(self) -> None:
        task, self.run_task = self.run_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _start_run(self, wait: bool, trigger: str) -> None:
        await self._cancel_run()
        self.run_task = self._create_task(
            self._plan_and_run(wait, trigger), "geodrops_rachio_run")

    # --- triggers ---------------------------------------------------------
    async def irrigation_nightly(self, _now=None) -> None:
        await self._start_run(True, "nightly")

    # _on_startup: port legacy 2604-2742 (see below)

    # --- button actions ---------------------------------------------------
    async def async_run_now(self) -> None:
        """Run the full plan immediately (no pre-dawn wait). Waters for real."""
        await self._start_run(False, "run_now")

    async def async_preview(self) -> None:
        """Dry run: report the plan that would execute, without watering."""
        await self._preview()

    async def request_stop(self) -> None:
        """The Stop button: raise the manual-stop flag the watch loop honours."""
        self._manual_stop = True
        await self._activity("Stop button pressed")

    # async_reset: port legacy 2766-2791 (see below)
    # async_refresh_runtimes: port legacy 2794-2834 (see below)

    # --- Home Assistant wiring ----------------------------------------------
    def async_start(self, hass: HomeAssistant) -> None:
        self._unsubs += [
            async_track_time_change(hass, self.irrigation_nightly,
                                    hour=23, minute=0, second=0),
            async_track_time_change(hass, self._on_calibrate_time,
                                    hour=6, minute=0, second=0),
            async_track_time_change(hass, self._on_settle_time,
                                    minute=[0, 30], second=0),
            async_at_started(hass, self._on_ha_started),
        ]

    async def _on_calibrate_time(self, _now) -> None:
        await self.irrigation_calibrate()

    async def _on_settle_time(self, _now) -> None:
        await self._settle_and_learn()

    async def _on_ha_started(self, _hass) -> None:
        self.startup_task = self._create_task(
            self._on_startup(), "geodrops_rachio_startup")

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        startup, self.startup_task = self.startup_task, None
        if startup is not None and not startup.done():
            startup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await startup
        was_watering = self._watering_active
        await self._cancel_run()
        if was_watering and self._current_cfg is not None:
            await self._safety_stop()

    async def _safety_stop(self) -> None:
        """Unload while valves were watering (spec: 'unload safety stop').

        Cancellation skips the runners' `except Exception` teardown, so a run
        cancelled mid-pause would leave Rachio to auto-resume the schedule with
        nobody watching. Stop the device and every zone, bounded.
        """
        bindings = self._current_bindings
        switches = [z.rachio_switch for z in self._current_cfg.zones.values()]
        try:
            async with asyncio.timeout(SAFETY_STOP_TIMEOUT_S):
                await self.port.call("rachio", "stop_watering",
                                     {"devices": bindings.rachio_device_name},
                                     blocking=True)
                for sw in switches:
                    await self.port.call("switch", "turn_off", {"entity_id": sw},
                                         blocking=True)
        except Exception as err:
            _LOGGER.warning(f"irrigation: unload safety stop failed ({err})")
```

Then port into `Scheduler`, per the Port rules:

| Method | Legacy lines | Notes |
|---|---|---|
| `_on_startup` | 2604–2742 | async. `task.sleep(30)` → `await self.port.sleep(30)`. `_restore_records()` (sync). Each `task.unique("geodrops_rachio_run"); _plan_and_run(wait=True, trigger="startup-heal")` → `await self._start_run(True, "startup-heal")`. `now = dt.datetime.now()` → `self._naive_now()`; `dt.datetime.now().isoformat()` → `self._naive_now().isoformat()` |
| `async_reset` | 2766–2791 | async. `task.unique(...)` → `await self._cancel_run()` |
| `async_refresh_runtimes` | 2794–2834 | async. `state.set("pyscript.geodrops_rachio_runtimes", …)` → `self._publish("runtimes", …)` |

Not ported: the `@service` wrappers (2745–2763) and `_on_stop_button` (2837–2847) — replaced by `async_run_now`, `async_preview`, `request_stop` above.

- [ ] **Step 5: Run**

Run: `bash tools/test.sh tests/engine -q`
Expected: PASS (all engine tests, incl. 9 new).

- [ ] **Step 6: Commit**

```bash
git add custom_components/geodrops_rachio/engine/scheduler.py tests/engine
git commit -m "feat(engine): Scheduler lifecycle, startup recovery, unload safety stop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Wire the engine into the integration; delete pyscript delivery

**Files:**
- Modify: `CC/__init__.py`, `CC/button.py`, `CC/sensor.py`, `CC/coordinator.py`, `CC/config_flow.py` (lines 26, 36, 734–736), `CC/config_writer.py`, `CC/rachio_client.py`, `CC/manifest.json`, `CC/strings.json`, `CC/translations/en.json`
- Delete: `CC/bundled_app/`, `CC/delivery.py`, `CC/updater.py`, `tests/test_delivery.py`, `tests/test_updater.py`
- Modify tests: `tests/test_init.py`, `tests/test_entities.py`, `tests/test_coordinator.py`, `tests/test_config_writer.py`, `tests/test_config_roundtrip.py`, `tests/test_config_flow.py`, `tests/conftest.py`, `tests/test_rachio_client.py`

**Interfaces:**
- Consumes: `Scheduler`, `HassPort`, `async_open_store`, `build_config`.
- Produces: `hass.data[DOMAIN][entry_id] = {"data", "coordinator", "scheduler"}`; `rachio_client.async_fetch_zone_data(session, key) -> tuple[dict, dict, dict]`; `ZoneStateCoordinator(hass, entry, scheduler)` with `data_for(key)`, `async_start()` (sync callback registration), `async_stop()`; `RecordSensor`; test helper `publish_record(hass, entry, name, value, attributes)` in `tests/conftest.py`.

- [ ] **Step 1: Write the failing integration tests** — replace `tests/test_init.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

DATA = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""}


@pytest.fixture(autouse=True)
def _config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture(autouse=True)
def _no_startup_sleep():
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
               AsyncMock()):
        yield


async def test_setup_and_unload_entry(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert "scheduler" in hass.data[DOMAIN][entry.entry_id]
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler.async_shutdown",
               AsyncMock()) as shutdown:
        assert await hass.config_entries.async_unload(entry.entry_id)
    shutdown.assert_awaited_once()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retires_delivered_pyscript(hass, tmp_path, enable_pyscript_and_rachio):
    ps = tmp_path / "pyscript"
    ps.mkdir()
    (ps / "geodrops_rachio.py").write_text("# legacy")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not (ps / "geodrops_rachio.py").exists()


async def test_setup_does_not_need_pyscript(hass):
    hass.config.components.add("rachio")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_bad_overrides_not_ready(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "advanced_overrides": "- a"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_nightly_trigger_starts_run(hass, enable_pyscript_and_rachio):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._start_run",
               AsyncMock()) as start:
        assert await hass.config_entries.async_setup(entry.entry_id)
        now = dt_util.now()
        async_fire_time_changed(hass, now.replace(hour=23, minute=0, second=0, microsecond=0)
                                + timedelta(days=1))
        await hass.async_block_till_done()
    start.assert_awaited_with(True, "nightly")
```

Keep the existing `test_reload_listener_honors_suppress_flag` test (moved below these), removing its `patch("...delivery.async_deliver", ...)` wrapper.

- [ ] **Step 2: Run to verify failure**

Run: `bash tools/test.sh tests/test_init.py -q`
Expected: FAIL (no `scheduler` in `hass.data`; delivery still runs).

- [ ] **Step 3: `rachio_client.async_fetch_zone_data`** — append to `CC/rachio_client.py`:

```python
async def async_fetch_zone_data(session, key: str) -> tuple[dict, dict, dict]:
    """(runtimes_minutes, refill_depths_mm, refill_spans_pts), each keyed by
    Rachio zone id, across every controller on the account — one pass over the
    device payloads (ported from the pyscript app's _fetch_zone_data). Raises on
    transport/HTTP/JSON errors; the engine then falls back to static values."""
    from .brain import rachio_runtime

    person = await _get_json(session, RACHIO_BASE + "person/info", key)
    payload = await _get_json(session, RACHIO_BASE + "person/" + person["id"], key)
    runtimes: dict = {}
    depths: dict = {}
    spans: dict = {}
    for device in payload["devices"]:
        dev = await _get_json(session, RACHIO_BASE + "device/" + device["id"], key)
        zones = dev.get("zones", [])
        runtimes.update(rachio_runtime.parse_runtimes(zones))
        depths.update(rachio_runtime.parse_refill_depths(zones))
        spans.update(rachio_runtime.parse_refill_spans(zones))
    return runtimes, depths, spans
```

Test — append to `tests/test_rachio_client.py`:

```python
async def test_async_fetch_zone_data(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.geodrops_rachio.rachio_client import (
        RACHIO_BASE, async_fetch_zone_data)
    aioclient_mock.get(RACHIO_BASE + "person/info", json={"id": "p1"})
    aioclient_mock.get(RACHIO_BASE + "person/p1", json={"devices": [{"id": "d1"}]})
    aioclient_mock.get(RACHIO_BASE + "device/d1", json={"zones": [
        {"id": "z1", "runtime": 1800, "depthOfWater": 0.5, "enabled": True}]})
    runtimes, depths, spans = await async_fetch_zone_data(
        async_get_clientsession(hass), "key")
    assert runtimes == {"z1": 30.0}
    assert round(depths["z1"], 1) == 12.7
    assert isinstance(spans, dict)
```

- [ ] **Step 4: Rewrite `CC/__init__.py`**:

```python
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import config_writer, rachio_client
from .const import DOMAIN, PLATFORMS
from .coordinator import ZoneStateCoordinator
from .engine.port import HassPort
from .engine.scheduler import Scheduler
from .engine.store import async_open_store
from .util import slug

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = dict(entry.data)
    try:
        config_writer.build_config(data)  # validates advanced_overrides
    except ValueError as err:
        raise ConfigEntryNotReady(str(err)) from err

    store = await async_open_store(hass, entry.entry_id)
    session = async_get_clientsession(hass)

    async def fetch_zone_data(key_name: str):
        key = await rachio_client.resolve_secret(hass, key_name)
        if not key:
            _LOGGER.warning(
                "irrigation: no %s in secrets.yaml; using static values", key_name)
            return {}, {}, {}
        return await rachio_client.async_fetch_zone_data(session, key)

    scheduler = Scheduler(
        HassPort(hass), store, lambda: config_writer.build_config(data),
        fetch_zone_data,
        lambda coro, name: entry.async_create_background_task(hass, coro, name))
    coordinator = ZoneStateCoordinator(hass, entry, scheduler)
    coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "data": data, "coordinator": coordinator, "scheduler": scheduler}
    entry.async_on_unload(coordinator.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    scheduler.async_start(hass)

    async def _on_stop(_event) -> None:
        await scheduler.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    _purge_orphan_zone_devices(hass, entry)
    return True
```

Keep `_purge_orphan_zone_devices` and `_reload_on_options` unchanged. Replace `async_unload_entry`:

```python
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is not None:
        await stored["scheduler"].async_shutdown()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return ok
```

- [ ] **Step 5: Rewrite `CC/coordinator.py`** — keep `format_calibration_status`, `_CONVERGENCE_TARGET`, `_REJECT_PHRASES`, and all `parse_*` functions unchanged. Delete the `*_ENTITY` constants, `STATE_DIRNAME`, `EFFICACY_STATE_FILE`, `_FILE_REFRESH`, `json`/`async_track_*` imports. Replace the class:

```python
class ZoneStateCoordinator:
    """Fans the scheduler's records + efficacy document out to per-zone sensors."""

    def __init__(self, hass: HomeAssistant, entry, scheduler) -> None:
        self.hass = hass
        self.entry = entry
        self._scheduler = scheduler
        self._listeners: list = []
        self._unsub = None

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _attrs(self, name: str) -> dict | None:
        rec = self._scheduler.records.get(name)
        return rec["attributes"] if rec is not None else None

    def data_for(self, key: str) -> dict:
        ...  # body below
```

`data_for` body: the previous body with these substitutions — `ln = self.hass.states.get(LAST_NIGHTLY_ENTITY)` → `ln = self._attrs("last_nightly")`, and every `ln.attributes` → `ln`; same for `tg` (`"targets"`), `rt` (`"runtimes"`), `pv` (`"preview"`); `out.update(parse_efficacy(self._efficacy, key))` → `out.update(parse_efficacy(self._scheduler.store.read(EFFICACY) or {}, key))` (import `EFFICACY` from `.engine.store`). Then:

```python
    @callback
    def async_start(self) -> None:
        self._unsub = self._scheduler.add_listener(self._notify)

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
```

- [ ] **Step 6: Buttons** — in `CC/button.py`, delete `_LOGGER` usage of pyscript and replace both classes:

```python
from .const import DOMAIN

_ACTIONS = [
    ("run_now", "Run irrigation now", "mdi:play-circle-outline"),
    ("preview", "Preview irrigation plan", "mdi:eye-outline"),
    ("reset", "Reset irrigation", "mdi:cancel"),
    ("refresh_runtimes", "Refresh Rachio runtimes", "mdi:refresh"),
]
# Button key -> Scheduler coroutine method. Long actions run as background tasks
# so a press never blocks on a preview or a Rachio fetch.
_METHODS = {
    "run_now": "async_run_now", "preview": "async_preview",
    "reset": "async_reset", "refresh_runtimes": "async_refresh_runtimes",
}
_BACKGROUND = {"preview", "refresh_runtimes"}


class StopButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, scheduler) -> None:
        self._attr_unique_id = f"{entry.entry_id}_stop"
        self._attr_name = "Stop irrigation"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_stop")
        self._attr_device_info = device_info(entry)
        self._scheduler = scheduler

    async def async_press(self) -> None:
        await self._scheduler.request_stop()


class ActionButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, scheduler, key: str, name: str, icon: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{key}")
        self._attr_device_info = device_info(entry)
        self._entry = entry
        self._scheduler = scheduler
        self._key = key

    async def async_press(self) -> None:
        coro = getattr(self._scheduler, _METHODS[self._key])()
        if self._key in _BACKGROUND:
            self._entry.async_create_background_task(
                self.hass, coro, f"geodrops_rachio_{self._key}")
        else:
            await coro


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    entities: list[ButtonEntity] = [StopButton(entry, scheduler)]
    entities += [ActionButton(entry, scheduler, key, name, icon)
                 for key, name, icon in _ACTIONS]
    async_add_entities(entities)
```

- [ ] **Step 7: Sensors** — in `CC/sensor.py`: delete `STATUS_ENTITY`; update the `_pretty_status` docstring sentence about "the pyscript.* source entities" to say the raw token is on the status sensor's `status` attribute. Replace `SchedulerStatusSensor` and add `RecordSensor`:

```python
from homeassistant.const import MATCH_ALL

class SchedulerStatusSensor(SensorEntity):
    """The scheduler's overall status (idle / planning / waiting / watering /
    standby / skipped / aborted) on the main device."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:sprinkler"

    def __init__(self, entry, scheduler) -> None:
        self._attr_unique_id = f"{entry.entry_id}_status"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_status")
        self._attr_device_info = device_info(entry)
        self._scheduler = scheduler

    async def async_added_to_hass(self) -> None:
        @callback
        def _update() -> None:
            rec = self._scheduler.records.get("status")
            attrs = rec["attributes"] if rec else {}
            raw = rec["value"] if rec else None
            self._attr_native_value = _pretty_status(raw)
            self._attr_extra_state_attributes = {
                "status": raw, "detail": attrs.get("detail"),
                "updated": attrs.get("updated")}
            self.async_write_ha_state()
        self.async_on_remove(self._scheduler.add_listener(_update))
        _update()


# (record name, entity suffix, display name, icon)
_RECORDS = [
    ("last_nightly", "last_nightly", "Last nightly run", "mdi:weather-night"),
    ("last_run", "last_run", "Last run", "mdi:history"),
    ("preview", "plan", "Plan", "mdi:eye-outline"),
]


class RecordSensor(SensorEntity):
    """One scheduler record: value as state, full record as attributes
    (same attribute names the pyscript.* entity carried, minus friendly_name).
    Attributes stay out of the recorder — they can be large."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry, scheduler, record, suffix, name, icon) -> None:
        self._attr_unique_id = f"{entry.entry_id}_record_{record}"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{suffix}")
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = device_info(entry)
        self._scheduler = scheduler
        self._record = record

    async def async_added_to_hass(self) -> None:
        @callback
        def _update() -> None:
            rec = self._scheduler.records.get(self._record)
            if rec is None:
                self._attr_native_value = None
                self._attr_extra_state_attributes = {}
            else:
                self._attr_native_value = rec["value"]
                self._attr_extra_state_attributes = {
                    k: v for k, v in rec["attributes"].items() if k != "friendly_name"}
            self.async_write_ha_state()
        self.async_on_remove(self._scheduler.add_listener(_update))
        _update()
```

In `async_setup_entry`: `scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]`; start the list with `SchedulerStatusSensor(entry, scheduler)` and append `RecordSensor(entry, scheduler, *r) for r in _RECORDS`.

Verify `MATCH_ALL` is honoured per-entity by this HA version:

Run: `docker run --rm geodrops-test python -c "import inspect, homeassistant.helpers.entity as e; print('MATCH_ALL' in inspect.getsource(e))"`
Expected: `True`. If `False`, list the record attribute keys explicitly instead (`frozenset({"pressure_forecast", "pressure_instant", "dosing", "calibration", "blocks", "breadcrumbs", "weather", "message", "priority", "skipped", "planned_minutes", "delivered_minutes", "uncompleted", "runtime_sources", "dosing_sources"})`).

- [ ] **Step 8: Config flow, config_writer, manifest, strings**

- `CC/config_flow.py`: line 26 `from .config_writer import generate_config` → `from .config_writer import build_config`; line 736 `generate_config(trial)` → `build_config(trial)`; the comment at 734 and the module docstring line 4 say `build_config`; line 36 → `REQUIRED_COMPONENTS = ("rachio",)`.
- `CC/config_writer.py`: delete `GENERATED_HEADER`, `generate_config`, and `import yaml` only if unused (it is still used for `advanced_overrides` parsing — keep it).
- `CC/manifest.json`: delete the `"after_dependencies": ["pyscript"],` entry (keys stay sorted).
- `CC/strings.json` and `CC/translations/en.json`: `"missing_prerequisites": "Install and set up the Rachio integration first, then add this integration."`

- [ ] **Step 9: Delete pyscript delivery**

```bash
git rm -r custom_components/geodrops_rachio/bundled_app custom_components/geodrops_rachio/delivery.py custom_components/geodrops_rachio/updater.py tests/test_delivery.py tests/test_updater.py
```

- [ ] **Step 10: Update the remaining tests**

- `tests/conftest.py` — add:

```python
def publish_record(hass, entry, name, value, attributes):
    """Test helper: publish a scheduler record as the engine would."""
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.data[DOMAIN][entry.entry_id]["scheduler"]._publish(name, value, attributes)
```

- `tests/test_entities.py`:
  - Replace the `_mock_delivery` fixture with:

    ```python
    @pytest.fixture(autouse=True)
    def _quiet_scheduler(hass, tmp_path):
        """Real entry setup, but no 30 s startup task and an isolated config dir."""
        hass.config.config_dir = str(tmp_path)
        with patch(
            "custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
            AsyncMock(),
        ):
            yield
    ```

  - Delete `test_action_button_calls_pyscript_service` and `test_action_button_no_raise_when_service_absent`; add:

    ```python
    async def test_action_button_calls_scheduler(hass, enable_pyscript_and_rachio):
        entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler.async_run_now",
                   AsyncMock()) as run_now:
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.geodrops_rachio_run_now"},
                blocking=True)
        run_now.assert_awaited_once()


    async def test_stop_button_raises_manual_stop(hass, enable_pyscript_and_rachio):
        entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.geodrops_rachio_stop"}, blocking=True)
        assert hass.data[DOMAIN][entry.entry_id]["scheduler"]._manual_stop is True


    async def test_record_sensors_mirror_scheduler(hass, enable_pyscript_and_rachio):
        from tests.conftest import publish_record
        entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        publish_record(hass, entry, "last_nightly", 2,
                       {"friendly_name": "x", "watered": ["front"], "aborted_reason": None})
        await hass.async_block_till_done()
        st = hass.states.get("sensor.geodrops_rachio_last_nightly")
        assert st.state == "2"
        assert st.attributes["watered"] == ["front"]
        assert st.attributes["friendly_name"] != "x"
        assert hass.states.get("sensor.geodrops_rachio_last_run") is not None
        assert hass.states.get("sensor.geodrops_rachio_plan") is not None
    ```

  - In `test_zone_status_sensors`, `test_calibration_state_shows_progress_and_reason`, `test_zone_deficit_sensor`, `test_refill_depth_prefers_live_rachio_value` and `test_scheduler_status_sensor_mirrors_pyscript` (rename it `..._mirrors_scheduler`): each currently calls `hass.states.async_set("pyscript.geodrops_rachio_<name>", "<v>", <attrs>)` **before** setup. Move that line to **after** `async_setup` + `async_block_till_done()`, as `publish_record(hass, entry, "<name>", <v as int>, <attrs>)` followed by `await hass.async_block_till_done()` (import `publish_record` from `tests.conftest`). Keep every assertion. In the status test also assert `st.attributes["status"] == "waiting"`.
- `tests/test_coordinator.py`: the `parse_*`/`format_*` tests are unchanged. In `test_remove_listener_stops_callbacks`, construct the coordinator with a scheduler stub:

    ```python
    from types import SimpleNamespace

    from custom_components.geodrops_rachio.engine.store import EngineStore


    async def _nosave(_docs):
        return None


    def _scheduler_stub(records=None, docs=None):
        return SimpleNamespace(records=records or {},
                               store=EngineStore(docs or {}, _nosave),
                               add_listener=lambda cb: (lambda: None))
    ```

    `ZoneStateCoordinator(hass, entry, _scheduler_stub())`. Add:

    ```python
    def test_data_for_reads_scheduler_records(hass):
        entry = SimpleNamespace(data={"zones": [{"key": "front", "rachio_zone_id": "z1",
                                                 "refill_depth_mm": 7.0}]})
        sched = _scheduler_stub(
            records={
                "last_nightly": {"value": 1, "attributes": {
                    "watered": ["front"], "delivered_minutes": {"front": 42.0},
                    "end_iso": "2026-09-13T06:00:00+00:00",
                    "calibration": {"front": {"state": "calibrating", "n_obs": 2}}}},
                "targets": {"value": 1, "attributes": {"target_floors": {"front": 65.0}}},
                "runtimes": {"value": 1, "attributes": {"refill_depths_mm": {"z1": 8.5}}},
                "preview": {"value": 1, "attributes": {"planned_minutes": {"front": 30}}},
            },
            docs={"efficacy": {"front": {"efficacy": 0.4}}})
        out = ZoneStateCoordinator(hass, entry, sched).data_for("front")
        assert out["last_delivered_runtime"] == 42.0
        assert out["target_floor"] == 65.0
        assert out["refill_depth"] == 8.5
        assert out["planned_runtime"] == 30
        assert out["efficacy"] == 0.4
        assert out["calibration_state"] == "Calibrating (2/3)"
    ```

- `tests/test_config_writer.py`: replace `yaml.safe_load(generate_config(X))` with `build_config(X)`; delete `test_header_present_and_flag_off`'s header assertion (keep its flag assertion); delete `test_build_config_matches_generated_yaml`.
- `tests/test_config_roundtrip.py`: import `build_config` and parse with `custom_components.geodrops_rachio.brain.config.parse_config` instead of the vendored `bundled_app` module; `raw = build_config(_wizard_data())`.
- `tests/test_config_flow.py:647`: comment says `build_config`; add a test that the user step proceeds when only `rachio` is loaded.

- [ ] **Step 11: Sweep for leftovers**

Run: `grep -rn "pyscript\|bundled_app\|delivery\|updater\|generate_config" custom_components tests tools --include=*.py --include=*.json | grep -v "tests/legacy/\|tests/engine/legacy_harness.py\|engine/store.py"`
Expected: only intentional mentions (the `pyscript.reload` retirement in `engine/store.py` is excluded; `tests/conftest.py`'s `enable_pyscript_and_rachio` fixture name may remain). Fix anything else.

- [ ] **Step 12: Run the full suite**

Run: `bash tools/test.sh -q`
Expected: all pass. Also `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain -q` → 279 passed.

- [ ] **Step 13: Commit**

```bash
git add -A custom_components tests
git commit -m "feat!: run the scheduler natively; remove pyscript delivery

BREAKING CHANGE: pyscript is no longer used. pyscript.geodrops_rachio_* entities
are replaced by sensor.geodrops_rachio_{status,last_nightly,last_run,plan}.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Docs, version, release prep

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `docs/RELEASING.md`, `docs/OPERATOR-VALIDATION.md`, `CC/manifest.json`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: README** — remove pyscript from Prerequisites (install steps, `allow_all_imports`), and describe the new entities (`sensor.geodrops_rachio_status` + `status` attribute, `_last_nightly`, `_last_run`, `_plan`). Add an "Upgrading from 0.9.x" section: HACS Download → restart; the integration deletes its old pyscript files and imports calibration history automatically; pyscript can be uninstalled if nothing else uses it; dashboards reading `pyscript.geodrops_rachio_last_nightly` must switch to `sensor.geodrops_rachio_last_nightly` (same attribute names), and `pyscript.geodrops_rachio_status` comparisons against lowercase tokens should read `state_attr('sensor.geodrops_rachio_status', 'status')`. Rollback: HACS Redownload v0.9.15 → restart.

- [ ] **Step 2: CHANGELOG + manifest** — `CC/manifest.json` `"version": "1.0.0"`; add a hand-written `## v1.0.0` section to `CHANGELOG.md` covering: native engine (pyscript no longer required), breaking entity changes, automatic migration + pyscript retirement, the unload safety stop fix, restart required, rollback.

- [ ] **Step 3: RELEASING.md / OPERATOR-VALIDATION.md** — replace the bundled_app / delivery / content-hash / "no restart for brain-only releases" guidance with: edit `brain/` or `engine/`, run both suites, manifest bump + hand-written notes + CHANGELOG, `gh release create`; every release needs an HA restart. Note the legacy oracle (`tests/legacy/`) is deleted in the first release after v1.0.0. Update OPERATOR-VALIDATION to the spec's cutover runbook.

- [ ] **Step 4: Verify**

Run: `bash tools/test.sh -q` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain -q`
Expected: all pass.

Run: `grep -rn "pyscript" README.md docs/RELEASING.md docs/OPERATOR-VALIDATION.md`
Expected: only upgrade/rollback mentions.

- [ ] **Step 5: Commit and push**

```bash
git add -A README.md CHANGELOG.md docs custom_components/geodrops_rachio/manifest.json
git commit -m "docs: v1.0.0 — native engine, upgrade + rollback notes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feature/native-engine
```

Then open the PR (body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`), wait for CI (tests + hassfest + HACS validation) to go green, and stop — the user merges. Release steps after merge (user-approved): tag `v1.0.0` from `main` with the CHANGELOG notes, `gh repo edit ajbaldwin/ha-geodrops-rachio-irrigation --remove-topic pyscript`, then run the spec's cutover runbook on the box.
