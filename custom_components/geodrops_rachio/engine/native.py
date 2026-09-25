"""Rachio-native runs: watering this app did not start.

A run from the Rachio app or a Rachio schedule reaches Home Assistant only as
zone switches turning on and off (Rachio webhooks). Those runs used to leave no
trace here: Last run and each zone's Last watered described only our own runs,
so a zone watered from Rachio several times a day (an overseed, excluded from
the nightly plan) read as untouched for weeks.

A session opens when a managed zone switch goes off -> on while this app is not
watering, and closes once every zone has been off for NATIVE_GAP_S (Rachio
pauses between the zones of one schedule). It is then published as Last run
with trigger `rachio`, each watered zone's latest watering is recorded, and any
pending calibration sample for those zones is dropped: its settle window now
holds water that sample would credit to the wrong run.

Tracking only. Planning needs nothing from it: the moisture sensors already
show the water, and crediting the minutes too would count it twice. Timing is
the switches', so it is only as good as the webhooks. The session is held in
memory; a restart mid-run loses that run.
"""
from __future__ import annotations

import logging

from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

# How long every zone must stay off before a Rachio session is over. Covers the
# pause between the zones of one Rachio schedule, so a multi-zone schedule is one
# Last run rather than one per zone.
NATIVE_GAP_S = 120


class NativeRunMixin:
    def _init_native_tracking(self) -> None:
        """Map each managed zone's Rachio switch to its zone key. A config that
        will not load disables tracking (the nightly run reports its own error)."""
        try:
            cfg = self._load_cfg()
        except Exception as err:
            _LOGGER.warning(
                f"irrigation: Rachio-native run tracking disabled; config load failed ({err})")
            self._native_switches = {}
            return
        self._native_switches = {z.rachio_switch: k for k, z in cfg.zones.items()}

    @callback
    def _on_switch_event(self, event) -> None:
        data = event.data
        old, new = data.get("old_state"), data.get("new_state")
        self._on_zone_switch(data["entity_id"], old.state if old is not None else None,
                             new.state if new is not None else None)

    def _on_zone_switch(self, switch, old, new) -> None:
        zone = self._native_switches.get(switch)
        if zone is None or old == new:
            return
        now = self.port.now()
        session = self._native_session
        if self._watering_active:
            # Our run owns the valves from here (the runner stops anything still
            # on before its first block): close any Rachio session at this instant.
            if session is not None:
                self._cancel_native_close()
                self._end_native_spans(session, now)
                self._native_session = None
                self._native_close_task = self._spawn_job(
                    self._finish_native_session(session), "geodrops_rachio_native")
            return
        if new == "on":
            if session is None:
                # Only a real off -> on is a start. A switch that ARRIVES on (HA
                # starting, the Rachio integration reloading) has an unknown start,
                # and a guessed one would under-report the run.
                if old != "off":
                    return
                session = self._native_session = {"start": now, "last_off": now, "zones": {}}
            else:
                self._cancel_native_close()
            span = session["zones"].setdefault(zone, {"on_since": None, "seconds": 0.0})
            if span["on_since"] is None:
                span["on_since"] = now
            return
        if session is None:
            return
        span = session["zones"].get(zone)
        if span is not None and span["on_since"] is not None:
            span["seconds"] += (now - span["on_since"]).total_seconds()
            span["on_since"] = None
            session["last_off"] = now
        if all(s["on_since"] is None for s in session["zones"].values()):
            self._cancel_native_close()
            self._native_close_task = self._spawn_job(
                self._close_native_after_gap(session), "geodrops_rachio_native")

    @staticmethod
    def _end_native_spans(session, now) -> None:
        for span in session["zones"].values():
            if span["on_since"] is not None:
                span["seconds"] += (now - span["on_since"]).total_seconds()
                span["on_since"] = None
                session["last_off"] = now

    def _cancel_native_close(self) -> None:
        task, self._native_close_task = self._native_close_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _close_native_after_gap(self, session) -> None:
        # Anything that ends or extends the session first cancels this task.
        await self.port.sleep(NATIVE_GAP_S)
        self._native_session = None
        await self._finish_native_session(session)

    async def _finish_native_session(self, session) -> None:
        zones = session["zones"]
        watered = [k for k, span in zones.items() if span["seconds"] > 0]
        if not watered:
            return
        minutes = {k: round(zones[k]["seconds"] / 60.0, 1) for k in watered}
        start, end = session["start"], session["last_off"]
        self._publish("last_run", len(watered), {
            "friendly_name": "Irrigation Last Run",
            "updated": self._naive_now().isoformat(timespec="seconds"),
            "trigger": "rachio",
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "end_iso": end.isoformat(),
            "watered": watered,
            "delivered_minutes": minutes,
        })
        await self._record_zone_watering(watered, minutes, end.isoformat(), "rachio")
        dropped = await self._drop_pending_obs(watered)
        detail = ", ".join([f"{k} {minutes[k]} min" for k in watered])
        await self._activity(
            f"Rachio run recorded — {detail} "
            f"({start.strftime('%H:%M')}–{end.strftime('%H:%M')})")
        if dropped:
            await self._activity(
                f"Rachio run: calibration sample dropped for {', '.join(dropped)} "
                "(watered again inside its settle window)")
