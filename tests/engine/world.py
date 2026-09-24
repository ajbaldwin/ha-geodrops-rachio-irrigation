"""A simulated Home Assistant + Rachio controller for engine tests.

The engine runs against a FakeWorld through FakePort, and each scenario's
observable effects (service calls, published records, persisted docs) are
compared with its golden fixture (see golden.py).

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

    def next_event_time(self) -> dt.datetime | None:
        """When the earliest scripted event is due, or None when none is left."""
        return self._events[0][0] if self._events else None

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
        """Sleep through scripted events, awaiting each event's coroutine AT its
        scheduled time (the clock stepped to that event, not to the end of the
        sleep) — as a real HA service call fired during a long `asyncio.sleep`
        runs at its own time. The legacy harness runs events synchronously at
        their time, so a time-sensitive async event (e.g. a preview fired during
        the pre-dawn wait) must see the same clock on both sides."""
        target = self.world.now() + dt.timedelta(seconds=seconds)
        while True:
            nxt = self.world.next_event_time()
            if nxt is None or nxt > target:
                break
            step = max(0.0, (nxt - self.world.now()).total_seconds())
            for coro in self.world.advance(step):
                await coro
        for coro in self.world.advance(max(0.0, (target - self.world.now()).total_seconds())):
            await coro
        await asyncio.sleep(0)

    def now(self):
        return self.world.now()
