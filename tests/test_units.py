import pytest

from custom_components.geodrops_rachio import units


@pytest.mark.parametrize(("value", "unit", "kind", "expected"), [
    (20.0, "°C", units.TEMPERATURE, 68.0),
    (68.0, "°F", units.TEMPERATURE, 68.0),
    (293.15, "K", units.TEMPERATURE, 68.0),
    (3.2186880, "km/h", units.SPEED, 2.0),
    (1.0, "m/s", units.SPEED, 2.236936),
    (2.0, "mph", units.SPEED, 2.0),
    (0.1, "in", units.PRECIPITATION, 2.54),
    (0.2, "cm", units.PRECIPITATION, 2.0),
    (0.5, "mm", units.PRECIPITATION, 0.5),
    # A gauge reporting the hour's rain as a rate: the depth over that hour.
    (0.1, "in/h", units.PRECIPITATION, 2.54),
    (0.5, "mm/h", units.PRECIPITATION, 0.5),
    (1.0, "in/d", units.PRECIPITATION, 25.4),
])
def test_converts_into_the_scheduler_units(value, unit, kind, expected):
    assert units.to_scheduler(value, unit, kind) == pytest.approx(expected)


@pytest.mark.parametrize(("unit", "kind"), [
    (None, units.TEMPERATURE),        # no unit: assumed already °F
    ("", units.SPEED),
    ("%", units.TEMPERATURE),         # unit of the wrong kind
    ("mm", units.SPEED),
    ("°C", None),                     # unitless kind (humidity)
])
def test_passes_through_what_it_cannot_convert(unit, kind):
    assert units.to_scheduler(12.5, unit, kind) == 12.5


def test_unit_of_reads_the_unit_attribute():
    assert units.unit_of({"unit_of_measurement": "°C"}) == "°C"
    assert units.unit_of({}) is None
