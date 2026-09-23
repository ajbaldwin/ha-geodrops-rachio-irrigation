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
