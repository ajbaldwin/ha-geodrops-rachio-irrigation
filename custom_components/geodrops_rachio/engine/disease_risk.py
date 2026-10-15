"""Refreshes the dollar spot assessment the planner reads (self.disease_risk).

Fails open: anything short of a usable assessment leaves disease_risk None,
which keeps every pressure breakdown and cap exactly as before dollar spot.
A reason is logged once each time the signal turns off, and once when it
comes back, not on every hourly refresh.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..brain import dollar_spot
from .base import EngineBase

_LOGGER = logging.getLogger(__name__)


class DiseaseRiskMixin(EngineBase):
    _disease_risk_off_reason: str | None = None

    async def async_refresh_disease_risk(self) -> None:
        risk: dollar_spot.DiseaseRisk | None = None
        reason: str | None = None
        try:
            cfg = self._load_cfg()
        except Exception as err:
            cfg = None
            reason = f"the configuration does not load ({err!r})"
        fetch = self._fetch_daily_means_fn
        if cfg is not None:
            if not cfg.tunables.dollar_spot_enabled:
                reason = "dollar_spot_enabled is false"
            elif fetch is None:
                reason = "no statistics source"
            else:
                w = cfg.bindings.weather
                today = self.port.now().date()
                first = today - dt.timedelta(days=dollar_spot.WINDOW_DAYS)
                try:
                    daily = await fetch(w.temperature, w.humidity, first, today)
                except Exception as err:
                    reason = f"the recorder statistics cannot be read ({err!r})"
                else:
                    risk = dollar_spot.assess(daily, today)
                    if risk is None:
                        reason = (
                            f"fewer than {dollar_spot.MIN_DAYS} of the last "
                            f"{dollar_spot.WINDOW_DAYS} days have daily statistics "
                            f"for {w.temperature} and {w.humidity}")
        if reason is not None and reason != self._disease_risk_off_reason:
            _LOGGER.info("Dollar spot pressure is off: %s", reason)
        elif reason is None and self._disease_risk_off_reason is not None:
            _LOGGER.info("Dollar spot pressure is on again")
        self._disease_risk_off_reason = reason
        if risk != self.disease_risk:
            self.disease_risk = risk
            self._notify_listeners()
