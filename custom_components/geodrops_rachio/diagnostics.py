"""Diagnostics: what a bug report needs, without the Rachio API key."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .coordinator import GeodropsRachioConfigEntry

# The key itself, and the secrets.yaml name entries before 1.2 read it from.
TO_REDACT = {CONF_API_KEY, "rachio_api_key_secret"}


async def async_get_config_entry_diagnostics(
        hass: HomeAssistant, entry: GeodropsRachioConfigEntry) -> dict[str, Any]:
    scheduler = entry.runtime_data.scheduler
    task = scheduler.run_task
    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "scheduler": {
            "run_in_flight": task is not None and not task.done(),
            "rachio_failing": scheduler._rachio_failing,
            "rachio_zone_cache": scheduler._runtime_cache,
            "records": scheduler.records,
            # Calibration history, run markers and persisted records.
            "store": scheduler.store.snapshot(),
        },
        "renamed_entities": entry.runtime_data.renamed,
    }
