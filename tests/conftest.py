import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def enable_pyscript_and_rachio(hass):
    """Register stub `pyscript`/`rachio` components so the config flow's
    prerequisite check passes without pulling in the real integrations."""
    hass.config.components.add("pyscript")
    hass.config.components.add("rachio")
    yield
    hass.config.components.remove("pyscript")
    hass.config.components.remove("rachio")


def publish_record(hass, entry, name, value, attributes):
    """Test helper: publish a scheduler record as the engine would."""
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.data[DOMAIN][entry.entry_id]["scheduler"]._publish(name, value, attributes)
