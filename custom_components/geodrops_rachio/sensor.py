from __future__ import annotations
import datetime as dt
import logging
from homeassistant.components.sensor import (
    RestoreSensor, SensorDeviceClass, SensorEntity, ENTITY_ID_FORMAT)
from homeassistant.const import (
    MATCH_ALL, PERCENTAGE, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory,
    UnitOfLength, UnitOfSpeed, UnitOfTemperature)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from . import units, weather_derive
from .coordinator import GeodropsRachioConfigEntry, ZoneStateCoordinator
from .engine.scheduler import Scheduler
from .entity import GeodropsRachioEntity, GeodropsRachioZoneEntity

_LOGGER = logging.getLogger(__name__)

# Pushed by the scheduler or computed from other entities; the forecast
# sensors call the weather entity themselves, on their own timer.
PARALLEL_UPDATES = 0

_FORECAST_INTERVAL = dt.timedelta(hours=1)
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


def _pretty_status(value):
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

    def as_dict(self) -> dict:
        return {"samples": [[t.isoformat(), v] for t, v in self.samples]}

    @staticmethod
    def parse(data: dict | None) -> list[tuple[dt.datetime, float]]:
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


class ObservedOvernightSensor(GeodropsRachioEntity, RestoreSensor):
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

    def _add_sample(self, state) -> None:
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
    def _on_source(self, event) -> None:
        self._add_sample(event.data.get("new_state"))
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _on_tick(self, _now) -> None:
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

    async def async_added_to_hass(self) -> None:
        if self._source:
            self.async_on_remove(async_track_time_interval(
                self.hass, self._refresh, _FORECAST_INTERVAL))
            await self._refresh(None)

    async def _refresh(self, _now) -> None:
        try:
            resp = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": self._source, "type": "hourly"},
                blocking=True, return_response=True)
            periods = (resp or {}).get(self._source, {}).get("forecast", [])
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
            return
        if not self._attr_available:
            _LOGGER.info("The hourly forecast of %s can be read again",
                         self._source)
        self._attr_available = True
        self._attr_native_value = value
        self.async_write_ha_state()

    def _forecast_unit(self) -> str | None:
        """The unit the weather entity reports this field in: its own
        `<field>_unit` attribute, else what a weather entity defaults to in
        Home Assistant's unit system."""
        attr = _FORECAST_UNIT_ATTR.get(self._field)
        if attr is None:
            return None
        st = self.hass.states.get(self._source)
        if st is not None and st.attributes.get(attr):
            return st.attributes[attr]
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


class ZoneMoistureSensor(GeodropsRachioZoneEntity, SensorEntity):
    """Live mirror of the zone's own GeoDrops dominant sensor; unavailable
    while that sensor is."""

    # GeoDrops dominant moisture is a 0-100 percentage; label it so readers see
    # "75.6 %" with a moisture icon instead of a bare number.
    _attr_device_class = SensorDeviceClass.MOISTURE
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, source: str | None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id,
                         "soil_moisture")
        self._source = source

    async def async_added_to_hass(self) -> None:
        @callback
        def _mirror(event=None) -> None:
            st = self.hass.states.get(self._source) if self._source else None
            # Unbound reads unknown rather than unavailable: nothing is down.
            self._attr_available = not self._source or not _source_down(st)
            # A moisture device_class must be numeric, so coerce and let
            # unknown/missing sources read as no value rather than pushing a
            # non-numeric state HA would reject.
            try:
                self._attr_native_value = float(st.state) if st else None
            except (TypeError, ValueError):
                self._attr_native_value = None
            self.async_write_ha_state()
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], _mirror))
        _mirror()


class ZoneDeficitSensor(GeodropsRachioZoneEntity, SensorEntity):
    """How far below its need-water target a zone currently is, in moisture
    points (%). Live: recomputes as the zone's moisture changes and when the
    scheduler republishes target floors. Value is max(0, target_floor -
    current) — 0 at/above the target. Reads unknown until both a floor (from
    the scheduler's targets state) and a numeric moisture reading exist. No
    device_class: it's a delta, not an absolute moisture, so HA must not unit-
    convert it."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str, coordinator: ZoneStateCoordinator,
                 source: str | None) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id, "deficit")
        self._key, self._coord, self._source = key, coordinator, source

    async def async_added_to_hass(self) -> None:
        self._coord.add_listener(self._update)
        self.async_on_remove(lambda: self._coord.remove_listener(self._update))
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], self._update))
        self._update()

    @callback
    def _update(self, event=None) -> None:
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
    ("preview", "plan"),
]


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


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    weather = entry.data.get("bindings", {}).get("weather", {})
    forecast_entity = entry.data.get("bindings", {}).get("forecast_entity")
    scheduler = entry.runtime_data.scheduler
    entities: list[SensorEntity] = [SchedulerStatusSensor(entry, scheduler)]
    entities += [RecordSensor(entry, scheduler, *r) for r in _RECORDS]
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
        entities.append(ZoneDeficitSensor(
            entry, z["key"], hub_id, coordinator, z.get("dominant_sensor")))
        for suffix, ckey, dc, unit, precision in _ZONE_FIELDS:
            entities.append(ZoneCoordinatorSensor(
                entry, z["key"], hub_id, coordinator, suffix, ckey, dc, unit,
                precision))

    async_add_entities(entities)
