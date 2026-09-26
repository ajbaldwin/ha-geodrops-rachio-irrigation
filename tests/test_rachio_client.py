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


def test_resolve_secret_text_numeric_value_is_stringified():
    assert resolve_secret_text("rachio_api_key: 12345\n", "rachio_api_key") == "12345"


def test_resolve_secret_text_non_mapping_returns_none():
    assert resolve_secret_text("- just\n- a list\n", "rachio_api_key") is None


async def test_resolve_secret_reads_the_config_secrets_file(hass, tmp_path):
    from custom_components.geodrops_rachio.rachio_client import resolve_secret
    hass.config.config_dir = str(tmp_path)
    assert await resolve_secret(hass, "rachio_api_key") is None      # no file yet
    (tmp_path / "secrets.yaml").write_text("rachio_api_key: K1\n", encoding="utf-8")
    assert await resolve_secret(hass, "rachio_api_key") == "K1"


async def test_async_fetch_devices_and_device_zones(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.geodrops_rachio.rachio_client import (
        RACHIO_BASE, async_fetch_device_zones, async_fetch_devices)
    aioclient_mock.get(RACHIO_BASE + "person/info", json={"id": "p1"})
    aioclient_mock.get(RACHIO_BASE + "person/p1", json={"devices": [
        {"id": "d1", "name": "Main House"}, {"name": "no id"}]})
    aioclient_mock.get(RACHIO_BASE + "device/d1", json={"zones": [
        {"id": "z1", "name": "Front", "zoneNumber": 1, "runtime": 600,
         "depthOfWater": 1.0, "enabled": True}]})
    session = async_get_clientsession(hass)
    assert await async_fetch_devices(session, "key") == [{"id": "d1", "name": "Main House"}]
    zones = await async_fetch_device_zones(session, "key", "d1")
    assert [(z["id"], z["runtime_minutes"]) for z in zones] == [("z1", 10.0)]
    # The API key travels as a bearer token on every hop.
    assert all(call[3]["Authorization"] == "Bearer key"
               for call in aioclient_mock.mock_calls)


async def test_async_fetch_zone_data_merges_every_controller(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.geodrops_rachio.rachio_client import (
        RACHIO_BASE, async_fetch_zone_data)
    aioclient_mock.get(RACHIO_BASE + "person/info", json={"id": "p1"})
    aioclient_mock.get(RACHIO_BASE + "person/p1",
                       json={"devices": [{"id": "d1"}, {"id": "d2"}]})
    aioclient_mock.get(RACHIO_BASE + "device/d1", json={"zones": [
        {"id": "z1", "runtime": 1800, "enabled": True}]})
    aioclient_mock.get(RACHIO_BASE + "device/d2", json={"zones": [
        {"id": "z2", "runtime": 600, "enabled": True}]})
    runtimes, _depths, _spans = await async_fetch_zone_data(
        async_get_clientsession(hass), "key")
    assert runtimes == {"z1": 30.0, "z2": 10.0}


async def test_async_fetch_zone_data_raises_on_an_http_error(hass, aioclient_mock):
    """A rejected key must raise, not return empty maps: the engine's fallback
    to static runtimes (and its warning) keys off the exception."""
    import aiohttp
    import pytest
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.geodrops_rachio.rachio_client import (
        RACHIO_BASE, async_fetch_zone_data)
    aioclient_mock.get(RACHIO_BASE + "person/info", status=401)
    with pytest.raises(aiohttp.ClientResponseError):
        await async_fetch_zone_data(async_get_clientsession(hass), "bad")
