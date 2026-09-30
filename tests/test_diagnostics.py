import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry)

from custom_components.geodrops_rachio.const import DOMAIN
from tests.conftest import publish_record

KEY = "sk-live-do-not-leak"
DATA = {"bindings": {"rachio_api_key_secret": "rachio_api_key"}, "zones": [],
        "self_calibration_enabled": False, "advanced_overrides": "",
        "api_key": KEY}


@pytest.fixture(autouse=True)
def _quiet_scheduler(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
               AsyncMock()):
        yield


async def test_diagnostics_redact_the_api_key(
        hass, hass_client, enable_pyscript_and_rachio):
    assert await async_setup_component(hass, "diagnostics", {})
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, minor_version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.rachio_client.async_fetch_account",
               AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    publish_record(hass, entry, "status", "idle", {"detail": "ready"})
    await entry.runtime_data.scheduler.store.write("efficacy", {"front": {"n_obs": 2}})

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert KEY not in json.dumps(diag)
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert diag["entry"]["data"]["bindings"]["rachio_api_key_secret"] == "**REDACTED**"
    assert diag["scheduler"]["records"]["status"]["value"] == "idle"
    assert diag["scheduler"]["store"]["efficacy"] == {"front": {"n_obs": 2}}
    assert diag["scheduler"]["run_in_flight"] is False
