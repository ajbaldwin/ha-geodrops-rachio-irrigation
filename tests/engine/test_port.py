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
