"""Weather readings into the scheduler's units: °F, mph and mm.

The brain's thresholds (brain/config.Tunables) are in °F, mph and mm, so every
weather reading is converted where it is read, from the unit its source
reports. A reading with no unit, or a unit that does not fit its kind, is taken
as already in the scheduler's unit — what every reading was assumed to be
before conversion existed.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT, UnitOfLength, UnitOfSpeed, UnitOfTemperature,
    UnitOfVolumetricFlux)
from homeassistant.util.unit_conversion import (
    DistanceConverter, SpeedConverter, TemperatureConverter)

TEMPERATURE = "temperature"
SPEED = "speed"
PRECIPITATION = "precipitation"

# A rain gauge may report its hourly or daily total as a rate; an hour's rain
# in mm/h is that hour's depth in mm, so a rate converts within its time base.
_FLUX_TO_MM: dict[str, str] = {
    UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR: UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
    UnitOfVolumetricFlux.INCHES_PER_HOUR: UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
    UnitOfVolumetricFlux.MILLIMETERS_PER_DAY: UnitOfVolumetricFlux.MILLIMETERS_PER_DAY,
    UnitOfVolumetricFlux.INCHES_PER_DAY: UnitOfVolumetricFlux.MILLIMETERS_PER_DAY,
}


def unit_of(attrs: Mapping[str, Any]) -> str | None:
    """The unit a state's attributes report, if any."""
    unit: str | None = attrs.get(ATTR_UNIT_OF_MEASUREMENT)
    return unit


def to_scheduler(value: float, unit: str | None, kind: str | None) -> float:
    """`value`, reported in `unit`, in the scheduler's unit for `kind`
    (°F for TEMPERATURE, mph for SPEED, mm for PRECIPITATION). A kind of None
    (humidity, probabilities) is unitless and passes through."""
    if kind is None or not unit:
        return value
    if kind == TEMPERATURE and unit in TemperatureConverter.VALID_UNITS:
        return TemperatureConverter.convert(
            value, unit, UnitOfTemperature.FAHRENHEIT)
    if kind == SPEED and unit in SpeedConverter.VALID_UNITS:
        return SpeedConverter.convert(value, unit, UnitOfSpeed.MILES_PER_HOUR)
    if kind == PRECIPITATION:
        if unit in DistanceConverter.VALID_UNITS:
            return DistanceConverter.convert(
                value, unit, UnitOfLength.MILLIMETERS)
        if unit in _FLUX_TO_MM:
            return SpeedConverter.convert(value, unit, _FLUX_TO_MM[unit])
    return value
