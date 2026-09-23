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

    # --- ported: legacy lines 146-212 -----------------------------------------
    async def _activity(self, message, entity_id=None):
        """Record a normal-operation event in the HA Logbook (the activity log),
        NOT the system log. The system log (log.warning/log.error) is reserved for
        errors and failures. Use this for routine events: runs, previews, refreshes,
        manual stops.

        Every entry is attached to an entity (`entity_id`, an optional field of
        logbook.log), defaulting to STATUS_ENTITY. Unattached entries can only be
        found by scrolling the global Logbook; attached ones can be filtered to, and
        show up in that entity's own more-info dialog. Pass a zone switch to file an
        event under the zone it happened to.
        """
        target = entity_id if entity_id is not None else STATUS_ENTITY
        await self.port.call("logbook", "log", {
            "name": LOGBOOK_NAME, "message": message, "entity_id": target})

    async def _notify(self, message, title):
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
                "notify", self._current_bindings.notify_service.split(".", 1)[-1],
                {"message": message, "title": title},
            )
        except Exception as err:
            _LOGGER.warning(
                f"irrigation: notification failed ({err}); run records were still written"
            )

    def _set_status(self, status, detail=None):
        """Publish what the scheduler is doing right now.

        HA logs a state CHANGE to the Logbook by itself, attached to the entity and
        carrying how long the previous state lasted — so this one call gives a
        filterable, duration-aware timeline of the night without a Logbook call per
        transition. It also answers a question nothing could before: mid-run, is the
        system waiting for the pre-dawn window or actually watering?

        States: idle / planning / waiting / watering / skipped / standby / aborted.
        A preview never touches it — a dry run changes nothing.

        NB pyscript re-creates its entities, so this reads `unknown` after a restart
        until the next run (or the startup handler) sets it.
        """
        attributes = {
            "friendly_name": "Irrigation Status",
            "updated": self._naive_now().isoformat(timespec="seconds"),
        }
        if detail is not None:
            attributes["detail"] = detail
        self._publish("status", status, attributes)
