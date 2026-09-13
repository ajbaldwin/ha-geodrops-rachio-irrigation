from __future__ import annotations
import datetime as dt
import logging
from homeassistant.components.sensor import SensorEntity, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_interval)
from . import weather_derive
from .entity_base import device_info

_LOGGER = logging.getLogger(__name__)

_OBSERVED_WINDOW = dt.timedelta(hours=12)
_FORECAST_INTERVAL = dt.timedelta(hours=1)
# (key suffix, forecast field, unit)
_FIELDS = [("temp", "temperature", "°F"), ("humidity", "humidity", "%"),
           ("wind", "wind_speed", "mph")]


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
    async_add_entities(entities)
