from __future__ import annotations
import datetime as dt
import logging
from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, ENTITY_ID_FORMAT)
from homeassistant.const import (
    MATCH_ALL, PERCENTAGE, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfLength)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)
from homeassistant.util import dt as dt_util
from . import weather_derive
from .const import DOMAIN
from .entity_base import device_info, zone_device_info
from .util import slug

_LOGGER = logging.getLogger(__name__)

_FORECAST_INTERVAL = dt.timedelta(hours=1)
# (key suffix, forecast field, unit)
_FIELDS = [("temp", "temperature", "°F"), ("humidity", "humidity", "%"),
           ("wind", "wind_speed", "mph")]

# (suffix, coordinator-key, device_class, unit, display_precision)
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


class ObservedOvernightSensor(SensorEntity):
    _attr_should_poll = False
    _attr_suggested_display_precision = 1

    def __init__(self, entry, key, source, unit) -> None:
        self._attr_unique_id = f"{entry.entry_id}_observed_overnight_{key}"
        self._attr_name = f"Observed overnight {key}"
        self.entity_id = ENTITY_ID_FORMAT.format(
            f"geodrops_rachio_observed_overnight_{key}")
        self._attr_native_unit_of_measurement = unit
        self._source = source
        self._samples: list[tuple[dt.datetime, float]] = []
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], self._on_source))

    @callback
    def _on_source(self, event) -> None:
        new = event.data.get("new_state")
        if new is None:
            return
        try:
            val = float(new.state)
        except (ValueError, TypeError):
            return
        # Local time: "overnight" is 20:00→06:00 in the home's timezone (the
        # scheduler calibrates at 06:00 local), not UTC — a UTC window would be
        # offset by hours for any non-UTC install.
        now = dt_util.now()
        self._samples.append((now, val))
        # Retain only samples from the current overnight window onward, then
        # average that window (the true 20:00→06:00 span, not a rolling 12h).
        start, _end = weather_derive.observed_overnight_window(now)
        self._samples = [(t, v) for t, v in self._samples if t >= start]
        self._attr_native_value = weather_derive.observed_overnight_mean(
            self._samples, now)
        self.async_write_ha_state()


class ForecastOvernightSensor(SensorEntity):
    _attr_should_poll = False
    _attr_suggested_display_precision = 1

    def __init__(self, entry, key, field, source, unit) -> None:
        self._attr_unique_id = f"{entry.entry_id}_forecast_overnight_{key}"
        self._attr_name = f"Forecast overnight {key}"
        self.entity_id = ENTITY_ID_FORMAT.format(
            f"geodrops_rachio_forecast_overnight_{key}")
        self._attr_native_unit_of_measurement = unit
        self._field = field
        self._source = source
        self._attr_device_info = device_info(entry)

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
        except Exception:
            _LOGGER.warning(
                "Failed to refresh forecast overnight sensor for %s",
                self._source, exc_info=True)
            return
        self._attr_native_value = value
        self.async_write_ha_state()


class ZoneCoordinatorSensor(SensorEntity):
    """Mirrors one field of the coordinator's per-zone state dict."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, key, coordinator, suffix, ckey,
                 device_class, unit, precision=None) -> None:
        s = slug(key)
        self._key, self._coord, self._ckey = key, coordinator, ckey
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_{suffix}"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_{suffix}")
        self._attr_name = suffix.replace("_", " ").capitalize()
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if precision is not None:
            self._attr_suggested_display_precision = precision
        self._attr_device_info = zone_device_info(entry, key)

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


class ZoneMoistureSensor(SensorEntity):
    """Live mirror of the zone's own GeoDrops dominant sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Soil moisture"
    # GeoDrops dominant moisture is a 0-100 percentage; label it so readers see
    # "75.6 %" with a moisture icon instead of a bare number.
    _attr_device_class = SensorDeviceClass.MOISTURE
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry, key, source) -> None:
        s = slug(key)
        self._source = source
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_soil_moisture"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_soil_moisture")
        self._attr_device_info = zone_device_info(entry, key)

    async def async_added_to_hass(self) -> None:
        @callback
        def _mirror(event=None) -> None:
            st = self.hass.states.get(self._source) if self._source else None
            # A moisture device_class must be numeric, so coerce and let
            # unknown/unavailable/missing sources read as no value rather than
            # pushing a non-numeric state HA would reject.
            try:
                self._attr_native_value = float(st.state) if st else None
            except (TypeError, ValueError):
                self._attr_native_value = None
            self.async_write_ha_state()
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], _mirror))
        _mirror()


class ZoneDeficitSensor(SensorEntity):
    """How far below its need-water target a zone currently is, in moisture
    points (%). Live: recomputes as the zone's moisture changes and when the
    scheduler republishes target floors. Value is max(0, target_floor -
    current) — 0 at/above the target. Reads unknown until both a floor (from
    the scheduler's targets state) and a numeric moisture reading exist. No
    device_class: it's a delta, not an absolute moisture, so HA must not unit-
    convert it."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Deficit"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry, key, coordinator, source) -> None:
        s = slug(key)
        self._key, self._coord, self._source = key, coordinator, source
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_deficit"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_deficit")
        self._attr_device_info = zone_device_info(entry, key)

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    weather = entry.data.get("bindings", {}).get("weather", {})
    forecast_entity = entry.data.get("bindings", {}).get("forecast_entity")
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    entities: list[SensorEntity] = [SchedulerStatusSensor(entry, scheduler)]
    entities += [RecordSensor(entry, scheduler, *r) for r in _RECORDS]
    for key, field, unit in _FIELDS:
        entities.append(ObservedOvernightSensor(
            entry, key, weather.get(field if field != "wind_speed" else "wind"), unit))
        entities.append(ForecastOvernightSensor(
            entry, key, field, forecast_entity, unit))

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    for z in entry.data.get("zones", []):
        entities.append(ZoneMoistureSensor(entry, z["key"], z.get("dominant_sensor")))
        entities.append(ZoneDeficitSensor(
            entry, z["key"], coordinator, z.get("dominant_sensor")))
        for suffix, ckey, dc, unit, precision in _ZONE_FIELDS:
            entities.append(ZoneCoordinatorSensor(
                entry, z["key"], coordinator, suffix, ckey, dc, unit, precision))

    async_add_entities(entities)
