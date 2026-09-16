from __future__ import annotations
import datetime as dt
import logging
from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, ENTITY_ID_FORMAT)
from homeassistant.const import PERCENTAGE
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

# (suffix, coordinator-key, device_class, unit)
_ZONE_FIELDS = [
    ("planned_runtime", "planned_runtime", SensorDeviceClass.DURATION, "min"),
    ("last_delivered_runtime", "last_delivered_runtime",
     SensorDeviceClass.DURATION, "min"),
    ("last_watered", "last_watered", SensorDeviceClass.TIMESTAMP, None),
    # Efficacy = moisture-points gained per minute of watering; no HA device
    # class fits, so just carry a unit for reader context.
    ("efficacy", "efficacy", None, f"{PERCENTAGE}/min"),
    # No device_class: avoids HA's enum-options validation churn.
    ("calibration_state", "calibration_state", None, None),
]


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
                 device_class, unit) -> None:
        s = slug(key)
        self._key, self._coord, self._ckey = key, coordinator, ckey
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_{suffix}"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_{suffix}")
        self._attr_name = suffix.replace("_", " ").capitalize()
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
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


STATUS_ENTITY = "pyscript.geodrops_rachio_status"


class SchedulerStatusSensor(SensorEntity):
    """Surfaces the scheduler's overall status (idle / planning / waiting /
    watering / standby / skipped / aborted) on the main device, mirroring the
    scheduler's own pyscript.geodrops_rachio_status entity."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:sprinkler"

    def __init__(self, entry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_status"
        self.entity_id = ENTITY_ID_FORMAT.format("geodrops_rachio_status")
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        @callback
        def _mirror(event=None) -> None:
            st = self.hass.states.get(STATUS_ENTITY)
            self._attr_native_value = st.state if st else None
            self._attr_extra_state_attributes = {
                "detail": st.attributes.get("detail") if st else None,
                "updated": st.attributes.get("updated") if st else None,
            }
            self.async_write_ha_state()
        self.async_on_remove(async_track_state_change_event(
            self.hass, [STATUS_ENTITY], _mirror))
        _mirror()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    weather = entry.data.get("bindings", {}).get("weather", {})
    forecast_entity = entry.data.get("bindings", {}).get("forecast_entity")
    entities: list[SensorEntity] = [SchedulerStatusSensor(entry)]
    for key, field, unit in _FIELDS:
        entities.append(ObservedOvernightSensor(
            entry, key, weather.get(field if field != "wind_speed" else "wind"), unit))
        entities.append(ForecastOvernightSensor(
            entry, key, field, forecast_entity, unit))

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    for z in entry.data.get("zones", []):
        entities.append(ZoneMoistureSensor(entry, z["key"], z.get("dominant_sensor")))
        for suffix, ckey, dc, unit in _ZONE_FIELDS:
            entities.append(ZoneCoordinatorSensor(
                entry, z["key"], coordinator, suffix, ckey, dc, unit))

    async_add_entities(entities)
