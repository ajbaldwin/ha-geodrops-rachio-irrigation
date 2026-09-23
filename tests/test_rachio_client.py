"""Unit tests for rachio_client: zone parsing and secret resolution, plus the
engine's zone-data fetch (against a mocked aiohttp session). The wizard's async
fetches are exercised through the config-flow tests."""
from custom_components.geodrops_rachio.rachio_client import (
    parse_devices,
    parse_zones,
    resolve_secret_text,
)


def test_parse_devices_maps_id_and_name():
    devices = [
        {"id": "dev-1", "name": "Main House", "status": "ONLINE"},
        {"id": "dev-2", "name": "Back Forty"},
    ]
    assert parse_devices(devices) == [
        {"id": "dev-1", "name": "Main House"},
        {"id": "dev-2", "name": "Back Forty"},
    ]


def test_parse_devices_skips_device_without_id():
    assert parse_devices([{"name": "No Id"}]) == []


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


async def test_async_fetch_zone_data(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.geodrops_rachio.rachio_client import (
        RACHIO_BASE, async_fetch_zone_data)
    aioclient_mock.get(RACHIO_BASE + "person/info", json={"id": "p1"})
    aioclient_mock.get(RACHIO_BASE + "person/p1", json={"devices": [{"id": "d1"}]})
    aioclient_mock.get(RACHIO_BASE + "device/d1", json={"zones": [
        {"id": "z1", "runtime": 1800, "depthOfWater": 0.5, "enabled": True}]})
    runtimes, depths, spans = await async_fetch_zone_data(
        async_get_clientsession(hass), "key")
    assert runtimes == {"z1": 30.0}
    assert round(depths["z1"], 1) == 12.7
    assert isinstance(spans, dict)
