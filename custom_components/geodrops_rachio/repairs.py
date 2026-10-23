"""Fix flows for the repairs that have one (see issues.py)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from . import issues
from .engine.store import EFFICACY


class ResumeCalibrationFlow(RepairsFlow):
    """zone_unresponsive: once the zones' heads and probes were checked,
    restart their calibration from a first probe."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _scheduler(self) -> Any:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        data = getattr(entry, "runtime_data", None) if entry is not None else None
        return getattr(data, "scheduler", None)

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        scheduler = self._scheduler()
        if user_input is not None:
            # An entry unloaded meanwhile has nothing to resume; its next setup
            # re-raises the repair if a zone is still unresponsive.
            if scheduler is not None:
                await scheduler.async_resume_calibration()
            return self.async_create_entry(data={})
        zones = (issues.unresponsive_zones(scheduler.store.read(EFFICACY))
                 if scheduler is not None else [])
        return self.async_show_form(
            step_id="confirm", description_placeholders={"zones": ", ".join(zones)})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return ResumeCalibrationFlow((data or {}).get("entry_id", ""))
