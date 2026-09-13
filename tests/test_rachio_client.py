"""Unit tests for the pure pieces of rachio_client: zone parsing and secret
resolution. The async HTTP fetch is exercised through the config-flow tests
(with a mocked session), not here."""
from custom_components.geodrops_rachio.rachio_client import (
    parse_zones,
    resolve_secret_text,
)


def test_parse_zones_maps_runtime_and_depth():
    zones = [
        {
            "id": "abc-1",
            "name": "Front Yard",
            "zoneNumber": 1,
            "runtime": 1800,  # seconds -> 30 min
            "depthOfWater": 0.5,  # inches -> 12.7 mm
            "enabled": True,
        }
    ]
    out = parse_zones(zones)
    assert out == [
        {
            "id": "abc-1",
            "name": "Front Yard",
            "zoneNumber": 1,
            "runtime_minutes": 30.0,
            "refill_depth_mm": 12.7,
        }
    ]


def test_parse_zones_skips_zone_without_id():
    zones = [{"name": "No Id", "runtime": 600, "enabled": True}]
    assert parse_zones(zones) == []


def test_parse_zones_skips_disabled_zone():
    zones = [
        {"id": "z1", "name": "On", "runtime": 600, "enabled": True},
        {"id": "z2", "name": "Off", "runtime": 600, "enabled": False},
    ]
    assert [z["id"] for z in parse_zones(zones)] == ["z1"]


def test_parse_zones_tolerates_missing_numeric_fields():
    """A zone with an id but a garbled runtime/depth still lists (id + name),
    with None for the values it could not parse, so the user can still pick it."""
    zones = [{"id": "z1", "name": "Weird", "runtime": "oops", "enabled": True}]
    out = parse_zones(zones)
    assert out[0]["id"] == "z1"
    assert out[0]["runtime_minutes"] is None
    assert out[0]["refill_depth_mm"] is None


def test_resolve_secret_text_returns_value():
    text = "rachio_api_key: SECRET123\nother: nope\n"
    assert resolve_secret_text(text, "rachio_api_key") == "SECRET123"


def test_resolve_secret_text_missing_key_returns_none():
    text = "other: nope\n"
    assert resolve_secret_text(text, "rachio_api_key") is None


def test_resolve_secret_text_malformed_returns_none():
    assert resolve_secret_text("::: not yaml :::", "rachio_api_key") is None


def test_resolve_secret_text_non_string_value_returns_none():
    """A non-scalar/non-string secret value is not a usable API key."""
    assert resolve_secret_text("rachio_api_key:\n  - 1\n", "rachio_api_key") is None
