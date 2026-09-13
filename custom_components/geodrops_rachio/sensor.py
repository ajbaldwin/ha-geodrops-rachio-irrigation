from __future__ import annotations
import datetime as dt
import logging
from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, ENTITY_ID_FORMAT)
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

_OBSERVED_WINDOW = dt.timedelta(hours=12)
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
    ("efficacy", "efficacy", None, None),
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
        now = dt.datetime.now(dt.timezone.utc)
        self._samples.append((now, val))
        cutoff = now - _OBSERVED_WINDOW
        self._samples = [(t, v) for t, v in self._samples if t >= cutoff]
        self._attr_native_value = weather_derive.overnight_mean(
            [v for _, v in self._samples])
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
                periods, self._field, dt.datetime.now(dt.timezone.utc))
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
        self._update()

    @callback
    def _update(self) -> None:
        value = self._coord.data_for(self._key).get(self._ckey)
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP and value:
            value = dt_util.parse_datetime(value)
        self._attr_native_value = value
        if self.hass:
            self.async_write_ha_state()


class ZoneMoistureSensor(SensorEntity):
    """Live mirror of the zone's own GeoDrops dominant sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Soil moisture"

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
            self._attr_native_value = st.state if st else None
            self.async_write_ha_state()
        if self._source:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._source], _mirror))
        _mirror()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    weather = entry.data.get("bindings", {}).get("weather", {})
    forecast_entity = entry.data.get("bindings", {}).get("forecast_entity")
    entities: list[SensorEntity] = []
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
