from __future__ import annotations
import datetime as dt
import logging
from typing import Any, cast
from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, ENTITY_ID_FORMAT)
from homeassistant.const import (
    MATCH_ALL, PERCENTAGE, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory,
    UnitOfLength, UnitOfSpeed, UnitOfTemperature)
from homeassistant.core import (
    CALLBACK_TYPE, Event, EventStateChangedData, HassJob, HomeAssistant, State,
    callback)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later, async_track_state_change_event, async_track_time_interval)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from . import owned_entities, units, weather_derive
from .brain.config import BAND_ORDER, enum_key
from .coordinator import GeodropsRachioConfigEntry, ZoneStateCoordinator
from .engine.scheduler import Scheduler
from .entity import GeodropsRachioEntity, GeodropsRachioZoneEntity
from .util import slug

_LOGGER = logging.getLogger(__name__)

# Pushed by the scheduler or computed from other entities; the forecast
# sensors call the weather entity themselves, on their own timer.
PARALLEL_UPDATES = 0

_FORECAST_INTERVAL = dt.timedelta(hours=1)
# While the forecast reads empty or fails (typically the weather integration
# still starting after a restart), retry this often, this many times, before
# falling back to the hourly refresh.
_FORECAST_RETRY_DELAY_S = 60
_FORECAST_RETRIES = 15
# How often the observed means recompute with nothing changing: a steady reading
# is still accruing time, and the 06:00 read should see the window's tail.
_OBSERVED_RECOMPUTE_INTERVAL = dt.timedelta(minutes=5)
# (key suffix, forecast field, device class, native unit, units kind). Native
# values are in the scheduler's units whatever the sources report. The device
# class lets HA show them in the home's units; the scheduler reads them back
# through units.to_scheduler, so the unit HA displays does not matter.
_FIELDS = [
    ("temp", "temperature", SensorDeviceClass.TEMPERATURE,
     UnitOfTemperature.FAHRENHEIT, units.TEMPERATURE),
    ("humidity", "humidity", SensorDeviceClass.HUMIDITY, PERCENTAGE, None),
    ("wind", "wind_speed", SensorDeviceClass.WIND_SPEED,
     UnitOfSpeed.MILES_PER_HOUR, units.SPEED),
]
# The weather entity attribute naming the unit its forecast reports a field in.
_FORECAST_UNIT_ATTR = {"temperature": "temperature_unit",
                       "wind_speed": "wind_speed_unit"}

# (suffix, coordinator-key, device_class, unit, display_precision). The
# calibration fields and refill depth are diagnostic (_ZONE_DIAGNOSTIC).
_ZONE_FIELDS = [
    ("planned_runtime", "planned_runtime", SensorDeviceClass.DURATION, "min", None),
    ("last_delivered_runtime", "last_delivered_runtime",
     SensorDeviceClass.DURATION, "min", None),
    ("last_watered", "last_watered", SensorDeviceClass.TIMESTAMP, None, None),
    # Efficacy = moisture-points gained per minute of watering; no HA device
    # class fits, so just carry a unit for reader context. Raw efficacy is a
    # long float — cap the DISPLAYED value at 3 decimals (state stays full).
    ("efficacy", "efficacy", None, f"{PERCENTAGE}/min", 3),
    # No device_class: avoids HA's enum-options validation churn.
    ("calibration_state", "calibration_state", None, None, None),
    # Rachio's per-zone "depth of water" (mm) — the refill this zone needs from
    # depletion back to field capacity. Config snapshot, overlaid with the live
    # Rachio value after a Refresh Runtimes pull (see coordinator). NO
    # device_class on purpose: SensorDeviceClass.DISTANCE makes HA convert mm to
    # the install's length unit (on an imperial box, 7 mm -> 0.28 in, which the
    # card rounds to "0"). Plain mm + a display precision keeps it readable.
    ("refill_depth", "refill_depth", None, UnitOfLength.MILLIMETERS, 1),
]
_ZONE_DIAGNOSTIC = {"efficacy", "calibration_state", "refill_depth"}


def _pretty_status(value: StateType) -> StateType:
    """Title-case a scheduler status/state word for display.

    The scheduler emits lowercase, underscore-joined tokens ("watering",
    "no_rise", "recalibrating"); shown raw they read as "calibrating", not
    "Calibrating". Title-case with underscores turned to spaces so both single
    words and multi-word tokens read cleanly, and pass non-strings through
    unchanged. Nothing consumes the lowercase form (the raw token is on the
    status sensor's `status` attribute), so this only affects how the state is
    displayed.
    """
    if not isinstance(value, str) or not value:
        return value
    # Leave HA's sentinel states alone: title-casing "unknown"/"unavailable"
    # would turn the special state into an ordinary string the UI mishandles.
    if value in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return value
    return value.replace("_", " ").title()


class _ObservedSamples(ExtraStoredData):
    """The observed sensor's samples, kept across a restart."""

    def __init__(self, samples: list[tuple[dt.datetime, float]]) -> None:
        self.samples = samples

    def as_dict(self) -> dict[str, Any]:
        return {"samples": [[t.isoformat(), v] for t, v in self.samples]}

    @staticmethod
    def parse(data: dict[str, Any] | None) -> list[tuple[dt.datetime, float]]:
        out = []
        for item in (data or {}).get("samples") or []:
            try:
                t = dt_util.parse_datetime(item[0])
                v = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if t is not None and t.tzinfo is not None:
                out.append((t, v))
        return sorted(out)


class ObservedOvernightSensor(GeodropsRachioEntity, SensorEntity, RestoreEntity):
    """Time-weighted mean of a weather reading over last night's window
    (weather_derive.observed_overnight_window), the observed side of the 06:00
    forecast calibration.

    Samples survive a restart (restore data), the source's reading at startup
    counts from then, and the value is recomputed on a timer as well as on each
    change — a steady reading is still accruing time when nothing changes.
    Each reading is converted from the source's unit as it arrives.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 source: str | None, device_class: SensorDeviceClass,
                 unit: str, kind: str | None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, f"observed_overnight_{key}")
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._source = source
        self._kind = kind
        self._samples: list[tuple[dt.datetime, float]] = []

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return _ObservedSamples(self._samples)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not self._source:
            return
        last = await self.async_get_last_extra_data()
        self._samples = _ObservedSamples.parse(last.as_dict() if last else None)
        self._add_sample(self.hass.states.get(self._source))
        self.async_on_remove(async_track_state_change_event(
            self.hass, [self._source], self._on_source))
        self.async_on_remove(async_track_time_interval(
            self.hass, self._on_tick, _OBSERVED_RECOMPUTE_INTERVAL))
        self._recompute()

    def _add_sample(self, state: State | None) -> None:
        if state is None:
            return
        try:
            val = float(state.state)
        except (ValueError, TypeError):
            return
        val = units.to_scheduler(val, units.unit_of(state.attributes), self._kind)
        # Local time: the window is in the home's timezone (the scheduler
        # calibrates at 06:00 local), not UTC.
        self._samples.append((dt_util.now(), val))

    def _recompute(self) -> None:
        now = dt_util.now()
        self._samples = weather_derive.prune_observed(self._samples, now)
        self._attr_native_value = weather_derive.observed_overnight_mean(
            self._samples, now)

    @callback
    def _on_source(self, event: Event[EventStateChangedData]) -> None:
        self._add_sample(event.data.get("new_state"))
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _on_tick(self, _now: dt.datetime) -> None:
        self._recompute()
        self.async_write_ha_state()


class ForecastOvernightSensor(GeodropsRachioEntity, SensorEntity):
    """Tonight's forecast mean of one weather field, from the bound weather
    entity's hourly forecast. Unavailable while that forecast cannot be read.

    The forecast comes in the weather entity's units (Home Assistant's unit
    system unless set on the entity) and is converted into this sensor's."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str, field: str,
                 source: str | None, device_class: SensorDeviceClass,
                 unit: str, kind: str | None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, f"forecast_overnight_{key}")
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._field = field
        self._source = source
        self._kind = kind
        self._retries_left = 0
        self._cancel_retry: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        if self._source:
            self.async_on_remove(async_track_time_interval(
                self.hass, self._refresh, _FORECAST_INTERVAL))
            # A weather entity coming up after this one (it often loads later
            # at startup) has a forecast to read now, not at the next hour.
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], self._on_source_change))
            self.async_on_remove(self._stop_retry)
            await self._refresh(None)

    @callback
    def _stop_retry(self) -> None:
        if self._cancel_retry is not None:
            self._cancel_retry()
            self._cancel_retry = None

    @callback
    def _on_source_change(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        down = (None, STATE_UNAVAILABLE, STATE_UNKNOWN)
        if ((old.state if old else None) in down
                and (new.state if new else None) not in down):
            self.hass.async_create_task(self._refresh(None))

    async def _retry(self, _now: dt.datetime) -> None:
        self._cancel_retry = None
        await self._refresh(None, retry=True)

    def _schedule_retry(self, retry: bool) -> None:
        """Try again in a minute after an empty or failed read, up to
        _FORECAST_RETRIES times in a row; a scheduled read (hourly, or the
        weather entity coming up) starts a fresh run of retries."""
        if not retry:
            self._retries_left = _FORECAST_RETRIES
        if self._retries_left <= 0 or self._cancel_retry is not None:
            return
        self._retries_left -= 1
        self._cancel_retry = async_call_later(
            self.hass, _FORECAST_RETRY_DELAY_S,
            HassJob(self._retry, cancel_on_shutdown=True))

    async def _refresh(self, _now: dt.datetime | None, retry: bool = False) -> None:
        if not self._source:
            return  # only scheduled for a bound weather entity
        if not retry:
            self._stop_retry()
        try:
            resp = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": self._source, "type": "hourly"},
                blocking=True, return_response=True)
            forecasts = cast(dict[str, Any], resp or {})
            periods = forecasts.get(self._source, {}).get("forecast", [])
            value = weather_derive.overnight_forecast_mean(
                periods, self._field, dt_util.now())
            if value is not None:
                value = units.to_scheduler(value, self._forecast_unit(), self._kind)
        except Exception as err:
            # Once per outage; the hourly retries log at debug.
            if self._attr_available:
                _LOGGER.info("The hourly forecast of %s cannot be read (%r); "
                             "%s is unavailable until it can", self._source,
                             err, self.entity_id)
            _LOGGER.debug("Forecast refresh for %s failed", self._source,
                          exc_info=True)
            self._attr_available = False
            self.async_write_ha_state()
            self._schedule_retry(retry)
            return
        if not self._attr_available:
            _LOGGER.info("The hourly forecast of %s can be read again",
                         self._source)
        self._attr_available = True
        self._attr_native_value = value
        self.async_write_ha_state()
        if value is None:
            # No hours of tonight in the forecast yet.
            self._schedule_retry(retry)

    def _forecast_unit(self) -> str | None:
        """The unit the weather entity reports this field in: its own
        `<field>_unit` attribute, else what a weather entity defaults to in
        Home Assistant's unit system."""
        attr = _FORECAST_UNIT_ATTR.get(self._field)
        if attr is None or not self._source:
            return None
        st = self.hass.states.get(self._source)
        if st is not None and st.attributes.get(attr):
            unit: str = st.attributes[attr]
            return unit
        if self._field == "temperature":
            return self.hass.config.units.temperature_unit
        # Weather entities default to km/h on a metric install, not the unit
        # system's m/s.
        if self.hass.config.units is METRIC_SYSTEM:
            return UnitOfSpeed.KILOMETERS_PER_HOUR
        return self.hass.config.units.wind_speed_unit


class ZoneCoordinatorSensor(GeodropsRachioZoneEntity, SensorEntity):
    """Mirrors one field of the coordinator's per-zone state dict."""

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, coordinator: ZoneStateCoordinator,
                 suffix: str, ckey: str, device_class: SensorDeviceClass | None,
                 unit: str | None, precision: int | None = None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id, suffix)
        self._key, self._coord, self._ckey = key, coordinator, ckey
        if suffix in _ZONE_DIAGNOSTIC:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if precision is not None:
            self._attr_suggested_display_precision = precision

    async def async_added_to_hass(self) -> None:
        self._coord.add_listener(self._update)
        self.async_on_remove(lambda: self._coord.remove_listener(self._update))
        self._update()

    @callback
    def _update(self) -> None:
        value = self._coord.data_for(self._key).get(self._ckey)
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP and value:
            parsed = dt_util.parse_datetime(value)
            # The scheduler stamps a naive local ISO time; a TIMESTAMP sensor
            # needs a tz-aware value, so localize a naive one.
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            value = parsed
        # calibration_state arrives already display-ready from the coordinator
        # (format_calibration_status), e.g. "Calibrating (2/3)" — no title-casing
        # here. The Status sensor still uses _pretty_status on its own value.
        self._attr_native_value = value
        if self.hass:
            self.async_write_ha_state()


def _source_down(state: State | None) -> bool:
    """A mirrored source entity that is missing or unavailable."""
    return state is None or state.state == STATE_UNAVAILABLE


class _ZoneMirrorSensor(GeodropsRachioZoneEntity, SensorEntity):
    """Live mirror of one of the zone's own GeoDrops sensors; unavailable
    while that sensor is."""

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, suffix: str, source: str | None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id, suffix)
        self._source = source

    def _value(self, state: State) -> StateType:
        """This sensor's value for the source's state; None for none."""
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        @callback
        def _mirror(event: Event[EventStateChangedData] | None = None) -> None:
            st = self.hass.states.get(self._source) if self._source else None
            # Unbound reads unknown rather than unavailable: nothing is down.
            self._attr_available = not self._source or not _source_down(st)
            self._attr_native_value = self._value(st) if st else None
            self.async_write_ha_state()
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], _mirror))
        _mirror()


class ZoneMoistureSensor(_ZoneMirrorSensor):
    """The zone's GeoDrops dominant moisture."""

    # GeoDrops dominant moisture is a 0-100 percentage; label it so readers see
    # "75.6 %" with a moisture icon instead of a bare number.
    _attr_device_class = SensorDeviceClass.MOISTURE
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, source: str | None) -> None:
        super().__init__(entry, key, hub_device_id, "soil_moisture", source)

    def _value(self, state: State) -> StateType:
        # A moisture device_class must be numeric, so coerce and let
        # unknown/missing sources read as no value rather than pushing a
        # non-numeric state HA would reject.
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None


class ZoneMoistureStateSensor(_ZoneMirrorSensor):
    """The zone's GeoDrops moisture state (Dry … Wet+), the band its Moisture
    target select is set in."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = BAND_ORDER

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, source: str | None) -> None:
        super().__init__(entry, key, hub_device_id, "moisture_state", source)

    def _value(self, state: State) -> StateType:
        # GeoDrops reports the band's label or its key, depending on version;
        # an enum sensor must hold one of its options.
        band = enum_key(state.state)
        return band if band in BAND_ORDER else None


class ZoneDeficitSensor(GeodropsRachioZoneEntity, SensorEntity):
    """How far below its need-water target a zone currently is, in moisture
    points (%). Live: recomputes as the zone's moisture changes and when the
    scheduler republishes target floors. Value is max(0, target_floor -
    current) — 0 at/above the target. Reads unknown until both a floor (from
    the scheduler's targets state) and a numeric moisture reading exist. No
    device_class: it's a delta, not an absolute moisture, so HA must not unit-
    convert it. Shown for an excluded zone too, with `excluded: true`: nothing
    waters it, but how dry it is still counts."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, coordinator: ZoneStateCoordinator,
                 source: str | None, exclude: str) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id, "deficit")
        self._key, self._coord, self._source = key, coordinator, source
        self._entry = entry
        self._exclude = exclude

    async def async_added_to_hass(self) -> None:
        # The exclude switch is this integration's own; follow a rename of it.
        self._exclude = owned_entities.resolver(
            self.hass, self._entry, self._entry.runtime_data.renamed)(self._exclude)
        self._coord.add_listener(self._update)
        self.async_on_remove(lambda: self._coord.remove_listener(self._update))
        watched = [e for e in (self._source, self._exclude) if e]
        self.async_on_remove(async_track_state_change_event(
            self.hass, watched, self._update))
        self._update()

    @callback
    def _update(self, event: Event[EventStateChangedData] | None = None) -> None:
        floor = self._coord.data_for(self._key).get("target_floor")
        moisture = None
        st = self.hass.states.get(self._source) if self._source else None
        # Down with the moisture sensor it is computed from.
        self._attr_available = not self._source or not _source_down(st)
        if st is not None:
            try:
                moisture = float(st.state)
            except (TypeError, ValueError):
                moisture = None
        if floor is None or moisture is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = round(max(0.0, floor - moisture), 1)
        excluded = self.hass.states.get(self._exclude) if self._exclude else None
        self._attr_extra_state_attributes = {
            "excluded": excluded is not None and excluded.state == "on"}
        if self.hass:
            self.async_write_ha_state()


class SchedulerStatusSensor(GeodropsRachioEntity, SensorEntity):
    """The scheduler's overall status (idle / planning / waiting / watering /
    standby / skipped / aborted) on the main device."""

    def __init__(self, entry: GeodropsRachioConfigEntry, scheduler: Scheduler) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "status")
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


# (record name, entity suffix); names and icons are translated by suffix.
_RECORDS = [
    ("last_nightly", "last_nightly"),
    ("last_run", "last_run"),
]


class WateringWindowSensor(GeodropsRachioEntity, SensorEntity):
    """Tonight's watering window as the next plan would size it, e.g.
    "02:10–05:45", recomputed whenever an input changes: the drought level,
    the Finish anchor and offset, the sun times, the overnight forecast.
    Attributes say how each end was arrived at."""

    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: GeodropsRachioConfigEntry,
                 scheduler: Scheduler) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, "watering_window")
        self._entry = entry
        self._scheduler = scheduler

    async def async_added_to_hass(self) -> None:
        @callback
        def _update(*_: Any) -> None:
            w = self._scheduler.project_window()
            if w is None:
                self._attr_native_value = None
                self._attr_extra_state_attributes = {}
            else:
                start = dt_util.as_local(w["start"])
                end = dt_util.as_local(w["end"])
                choice = w["anchor_choice"]
                self._attr_native_value = (
                    f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}")
                self._attr_extra_state_attributes = {
                    "start": start.isoformat(), "end": end.isoformat(),
                    "finish_anchor": w["anchor"],
                    "finish_anchor_source": (
                        "Finish anchor" if choice in ("dawn", "sunrise")
                        else "drought level"),
                    # Later-is-positive, like the Finish offset slider.
                    "minutes_after_anchor": -w["end_offset_minutes"],
                    "finish_offset": w["shift_minutes"],
                    "window_hours": w["cap_hours"],
                    "sized_from": w["cap_source"],
                    "disease_pressure": w["pressure"],
                    "drought_level": w["drought_level"],
                }
            self.async_write_ha_state()

        resolve = owned_entities.resolver(
            self.hass, self._entry, self._entry.runtime_data.renamed)
        inputs = sorted({resolve(e) for e in self._scheduler.window_inputs()})
        if inputs:
            self.async_on_remove(async_track_state_change_event(
                self.hass, inputs, _update))
        # And on each scheduler publish: the safety net for an input renamed
        # after this subscribed.
        self.async_on_remove(self._scheduler.add_listener(_update))
        _update()


class RecordSensor(GeodropsRachioEntity, SensorEntity):
    """One scheduler record: value as state, full record as attributes
    (the same attribute names v0.9.x's pyscript.* entities carried, minus
    friendly_name).
    Attributes stay out of the recorder — they can be large."""

    # Each record's state is a zone count (watered, or planned for Plan).
    _attr_native_unit_of_measurement = "zones"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: GeodropsRachioConfigEntry, scheduler: Scheduler,
                 record: str, suffix: str) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, f"record_{record}", suffix)
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


class PlanSensor(RecordSensor):
    """Tonight's plan: what a run would water, kept current without pressing
    Preview. The scheduler refreshes it hourly and after a restart, a run
    shows its own plan here, and changing a setting that moves the plan (the
    drought level, the Finish controls, standby, a zone's exclude switch)
    refreshes it at once. The `source` attribute says which made it."""

    def __init__(self, entry: GeodropsRachioConfigEntry, scheduler: Scheduler,
                 record: str, suffix: str) -> None:
        super().__init__(entry, scheduler, record, suffix)
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        resolve = owned_entities.resolver(
            self.hass, self._entry, self._entry.runtime_data.renamed)
        inputs = sorted({resolve(e) for e in self._scheduler.plan_inputs()})
        if inputs:
            @callback
            def _changed(_event: Event[EventStateChangedData]) -> None:
                self._scheduler.request_plan_refresh()
            self.async_on_remove(async_track_state_change_event(
                self.hass, inputs, _changed))


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    weather = entry.data.get("bindings", {}).get("weather", {})
    forecast_entity = entry.data.get("bindings", {}).get("forecast_entity")
    scheduler = entry.runtime_data.scheduler
    entities: list[SensorEntity] = [SchedulerStatusSensor(entry, scheduler)]
    entities += [RecordSensor(entry, scheduler, *r) for r in _RECORDS]
    entities.append(PlanSensor(entry, scheduler, "preview", "plan"))
    entities.append(WateringWindowSensor(entry, scheduler))
    for key, field, device_class, unit, kind in _FIELDS:
        entities.append(ObservedOvernightSensor(
            entry, key, weather.get(field if field != "wind_speed" else "wind"),
            device_class, unit, kind))
        entities.append(ForecastOvernightSensor(
            entry, key, field, forecast_entity, device_class, unit, kind))

    coordinator = entry.runtime_data.coordinator
    hub_id = entry.runtime_data.hub_device_id
    for z in entry.data.get("zones", []):
        entities.append(ZoneMoistureSensor(
            entry, z["key"], hub_id, z.get("dominant_sensor")))
        entities.append(ZoneMoistureStateSensor(
            entry, z["key"], hub_id, z.get("state_sensor")))
        entities.append(ZoneDeficitSensor(
            entry, z["key"], hub_id, coordinator, z.get("dominant_sensor"),
            z.get("exclude_boolean")
            or f"switch.geodrops_rachio_{slug(z['key'])}_exclude"))
        for suffix, ckey, dc, zone_unit, precision in _ZONE_FIELDS:
            entities.append(ZoneCoordinatorSensor(
                entry, z["key"], hub_id, coordinator, suffix, ckey, dc, zone_unit,
                precision))

    async_add_entities(entities)
