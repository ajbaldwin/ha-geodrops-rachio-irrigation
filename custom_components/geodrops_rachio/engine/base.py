"""Engine base: the former pyscript app's module globals as instance state.

Ported from v0.9.15 bundled_app/geodrops_rachio.py lines 121-213 and 1535-1571. The
app kept its run state in module globals because pyscript callbacks could not
close over locals; here it is plain instance state on one object.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import deque
from typing import Any, Awaitable, Callable

from homeassistant.exceptions import ServiceNotFound

from ..brain import config
from .port import HAPort
from .store import PERSISTED_RECORDS, EngineStore, record_key

_LOGGER = logging.getLogger(__name__)

STATUS_ENTITY = "sensor.geodrops_rachio_status"
LOGBOOK_NAME = "Irrigation"
# A night publishes a few dozen records; this holds several nights' worth.
RECORD_HISTORY_MAX = 500

# (runtimes_minutes, refill_depths_mm, refill_spans_pts), each keyed by Rachio
# zone id: one live pull (rachio_client.async_fetch_zone_data).
type ZoneData = tuple[dict[str, float], dict[str, float], dict[str, float]]


class EngineBase:
    def __init__(self, port: HAPort, store: EngineStore,
                 load_raw_config: Callable[[], dict[str, Any]],
                 fetch_zone_data: Callable[[], Awaitable[ZoneData]]) -> None:
        self.port = port
        self.store = store
        self._load_raw_config = load_raw_config
        self._fetch_zone_data_fn = fetch_zone_data
        self._manual_stop = False
        self.api_calls = 0     # Rachio-affecting service calls (start/stop) — the real budget
        self.state_polls = 0   # local HA state reads (free); tracked separately
        self._runtime_cache: dict[str, Any] = {
            "ts": 0.0, "runtimes": {}, "depths": {}, "spans": {}}
        # The last Rachio fetch failed; logged once until one succeeds.
        self._rachio_failing = False
        # The config the current entry point loaded (None before the first);
        # read through _loaded_cfg() and friends.
        self._current_tun: config.Tunables | None = None
        self._current_cfg: config.Config | None = None
        self._current_bindings: config.HABindings | None = None
        self._run_in_progress = False
        self._watering_active = False
        self._rain_since: float | None = None
        # Serialises read-modify-writes of the pending calibration queue (the
        # settle poll awaits a fetch between its read and its write).
        self._obs_lock = asyncio.Lock()
        # Valve closes the runner's polls have seen during the current run
        # (see IOMixin._note_valve): switches last seen on, and switch -> when
        # it was first seen off after that.
        self._valve_on: set[str] = set()
        self._valve_closed: dict[str, dt.datetime] = {}
        self.records: dict[str, dict[str, Any]] = {}
        # Recent publishes, oldest first — a debugging/test trail, so bounded: the
        # engine lives as long as Home Assistant does.
        self.record_history: deque[tuple[str, Any, dict[str, Any]]] = deque(
            maxlen=RECORD_HISTORY_MAX)
        self._listeners: list[Callable[[], None]] = []
        self.store.on_write = self._notify_listeners
        # Sensors get last night's records immediately, not 30 s after startup.
        for name in PERSISTED_RECORDS:
            doc = self.store.read(record_key(name))
            if doc:
                self.records[name] = doc

    # --- plumbing ------------------------------------------------------------
    def _load_cfg(self) -> config.Config:
        return config.parse_config(self._load_raw_config())

    def _loaded_cfg(self) -> config.Config:
        """The config the current entry point loaded; every path that reads
        it loads one first."""
        if self._current_cfg is None:
            raise RuntimeError("no scheduler config loaded")
        return self._current_cfg

    def _loaded_bindings(self) -> config.HABindings:
        """The loaded config's bindings (see _loaded_cfg)."""
        if self._current_bindings is None:
            raise RuntimeError("no scheduler config loaded")
        return self._current_bindings

    def _naive_now(self) -> dt.datetime:
        return self.port.now().replace(tzinfo=None)

    def _state_get(self, entity_id: str) -> str:
        """`self.port.state(entity_id)`, but RAISE `NameError(entity_id)` when the
        entity does not exist — matching pyscript's `state.get`.

        Use this (not `self.port.state`) at any ported call site whose legacy
        `state.get(e)` is a BARE read: no enclosing `except NameError:` that turns
        a missing entity into an explicit fallback. `self.port.state` degrading to
        `None` there would silently change behaviour (e.g. a zone reading as
        merely "unavailable" instead of aborting the run) instead of raising, same
        as the legacy call would have, into whatever broader `except Exception`
        (or nothing at all) was around it.
        """
        value = self.port.state(entity_id)
        if value is None:
            raise NameError(entity_id)
        return value

    def _last_updated_get(self, entity_id: str) -> dt.datetime:
        """`self.port.last_updated(entity_id)`, but RAISE `NameError(entity_id)`
        when the entity does not exist — the `.last_updated` counterpart to
        `_state_get` (mirrors pyscript's `state.get(e + ".last_updated")`)."""
        value = self.port.last_updated(entity_id)
        if value is None:
            raise NameError(entity_id)
        return value

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
        return _remove

    def _notify_listeners(self) -> None:
        # Listeners are entity updates called synchronously from engine code; a
        # failing one must never break the run that published the record.
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("record listener %r failed", cb)

    def _publish(self, name: str, value: Any, attributes: dict[str, Any]) -> None:
        self.records[name] = {"value": value, "attributes": attributes}
        self.record_history.append((name, value, attributes))
        self._notify_listeners()

    async def _publish_record(self, name: str, value: Any,
                              attributes: dict[str, Any]) -> None:
        """Publish a diagnostic state AND persist it, so a restart cannot erase it.

        A persistence failure must never take down the run that produced the
        record, so it warns and continues — the in-memory state is still
        published either way.
        """
        self._publish(name, value, attributes)
        try:
            await self.store.write(record_key(name), {"value": value, "attributes": attributes})
        except Exception as err:
            _LOGGER.warning("could not persist %s (%s)", name, err)

    def _restore_records(self) -> None:
        """Re-publish persisted diagnostic states after a restart."""
        for name in PERSISTED_RECORDS:
            data = self.store.read(record_key(name))
            if not data:
                continue
            self._publish(name, data["value"], data["attributes"])

    # --- ported: legacy lines 146-212 -----------------------------------------
    async def _activity(self, message: str, entity_id: str | None = None) -> None:
        """Record a normal-operation event in the HA Logbook (the activity log),
        NOT the system log. The system log (log.warning/log.error) is reserved for
        errors and failures. Use this for routine events: runs, previews, refreshes,
        manual stops.

        Every entry is attached to an entity (`entity_id`, an optional field of
        logbook.log), defaulting to STATUS_ENTITY. Unattached entries can only be
        found by scrolling the global Logbook; attached ones can be filtered to, and
        show up in that entity's own more-info dialog. Pass a zone switch to file an
        event under the zone it happened to.

        A Home Assistant without the logbook integration (it ships in
        default_config, but can be left out) just loses the entry; the action
        that logged it must still happen.
        """
        target = entity_id if entity_id is not None else STATUS_ENTITY
        try:
            await self.port.call("logbook", "log", {
                "name": LOGBOOK_NAME, "message": message, "entity_id": target})
        except ServiceNotFound:
            _LOGGER.debug("logbook not loaded; not recorded: %s", message)

    async def _notify(self, message: str, title: str) -> None:
        """Send a push, tolerant of a missing/renamed notify service.

        A notify failure must cost only the push — never the run's records or a
        calendar entry that may follow it in the same recap (the calendar write
        already lives by this rule; see _report). So swallow and log rather than let
        it propagate: an exception here would otherwise skip the calendar entry that
        runs after it and unwind out of the recap. The push is the most external and
        least important thing the run does — HA integrations rename notify services
        out from under a static binding (e.g. legacy notify.mobile_app_* → notify
        entities), and that must degrade to a logged warning, not a lost recap.
        """
        try:
            await self.port.call(
                "notify", self._loaded_bindings().notify_service.split(".", 1)[-1],
                {"message": message, "title": title},
            )
        except Exception as err:
            _LOGGER.warning(
                "notification failed (%s); run records were still written", err
            )

    def _set_status(self, status: str, detail: str | None = None) -> None:
        """Publish what the scheduler is doing right now.

        HA logs a state CHANGE to the Logbook by itself, attached to the entity and
        carrying how long the previous state lasted — so this one call gives a
        filterable, duration-aware timeline of the night without a Logbook call per
        transition. It also answers a question nothing could before: mid-run, is the
        system waiting for the pre-dawn window or actually watering?

        States: idle / planning / waiting / watering / skipped / standby / aborted.
        A preview never touches it — a dry run changes nothing.

        NB status is not persisted, so it reads `unknown` after a restart until the
        next run (or the startup handler) sets it.
        """
        attributes = {
            "friendly_name": "Irrigation Status",
            "updated": self._naive_now().isoformat(timespec="seconds"),
        }
        if detail is not None:
            attributes["detail"] = detail
        self._publish("status", status, attributes)
