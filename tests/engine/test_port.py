from custom_components.geodrops_rachio.engine.port import HassPort


async def test_hass_port_reads_and_calls(hass):
    port = HassPort(hass)
    assert port.state("sensor.nope") is None
    assert port.attrs("sensor.nope") == {}
    hass.states.async_set("sensor.x", "42", {"unit": "%"})
    assert port.state("sensor.x") == "42"
    assert port.attrs("sensor.x") == {"unit": "%"}
    assert port.last_updated("sensor.x") is not None

    calls = []
    hass.services.async_register("test", "echo", lambda call: calls.append(call.data))
    await port.call("test", "echo", {"a": 1}, blocking=True)
    assert calls == [{"a": 1}]
    assert port.now().tzinfo is not None


async def test_hass_port_resolves_entity_ids(hass):
    port = HassPort(hass, lambda e: {"switch.old": "switch.new"}.get(e, e))
    hass.states.async_set("switch.new", "on", {"a": 1})
    assert port.state("switch.old") == "on"
    assert port.attrs("switch.old") == {"a": 1}
    assert port.last_updated("switch.old") is not None
    assert port.state("switch.other") is None

    calls = []
    hass.services.async_register("test", "echo", lambda call: calls.append(call.data))
    await port.call("test", "echo", {"entity_id": "switch.old"}, blocking=True)
    await port.call("test", "echo", {"entity_id": ["switch.old", "switch.x"]},
                    blocking=True)
    assert calls == [{"entity_id": "switch.new"},
                     {"entity_id": ["switch.new", "switch.x"]}]


async def test_hass_port_finds_the_geodrops_last_reading_sibling(hass):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="geodrops")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("geodrops", "SN1")})
    reg = er.async_get(hass)
    moisture = reg.async_get_or_create(
        "sensor", "geodrops", "SN1_moisture", device_id=device.id,
        config_entry=entry)
    reg.async_get_or_create("sensor", "geodrops", "SN1_battery", device_id=device.id,
                            config_entry=entry)
    last = reg.async_get_or_create(
        "sensor", "geodrops", "SN1_last_reading", device_id=device.id,
        config_entry=entry)
    other = reg.async_get_or_create("sensor", "template", "front_mirror")

    port = HassPort(hass, lambda e: {"sensor.old": moisture.entity_id}.get(e, e))
    assert port.reading_time_entity(moisture.entity_id) == last.entity_id
    assert port.reading_time_entity("sensor.old") == last.entity_id   # resolved
    assert port.reading_time_entity(other.entity_id) is None          # not GeoDrops
    assert port.reading_time_entity("sensor.nope") is None            # unregistered


async def test_hass_port_finds_the_geodrops_active_switch(hass):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="geodrops")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    reg = er.async_get(hass)
    probe = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("geodrops", "SN1")})
    moisture = reg.async_get_or_create(
        "sensor", "geodrops", "SN1_moisture", device_id=probe.id, config_entry=entry)
    # Another switch whose key merely ends the same way, registered first.
    reg.async_get_or_create("switch", "geodrops", "SN1_alerts_active",
                            device_id=probe.id, config_entry=entry)
    reg.async_get_or_create("sensor", "geodrops", "SN1_previous_last_reading",
                            device_id=probe.id, config_entry=entry)
    last = reg.async_get_or_create("sensor", "geodrops", "SN1_last_reading",
                                   device_id=probe.id, config_entry=entry)
    active = reg.async_get_or_create(
        "switch", "geodrops", "SN1_active", device_id=probe.id, config_entry=entry)
    # A GeoDrops version without the switch; a non-switch entity whose unique
    # id happens to end the same way is not it.
    older = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("geodrops", "SN2")})
    old_moisture = reg.async_get_or_create(
        "sensor", "geodrops", "SN2_moisture", device_id=older.id, config_entry=entry)
    reg.async_get_or_create("binary_sensor", "geodrops", "SN2_active",
                            device_id=older.id, config_entry=entry)
    other = reg.async_get_or_create("sensor", "template", "front_mirror")

    port = HassPort(hass, lambda e: {"sensor.old": moisture.entity_id}.get(e, e))
    assert port.probe_active_entity(moisture.entity_id) == active.entity_id
    assert port.reading_time_entity(moisture.entity_id) == last.entity_id
    assert port.probe_active_entity("sensor.old") == active.entity_id   # resolved
    assert port.probe_active_entity(old_moisture.entity_id) is None     # no switch
    assert port.probe_active_entity(other.entity_id) is None            # not GeoDrops
    assert port.probe_active_entity("sensor.nope") is None              # unregistered
