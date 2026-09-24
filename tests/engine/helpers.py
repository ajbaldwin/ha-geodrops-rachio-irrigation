"""Shared helpers for the engine tests."""
from __future__ import annotations


def prime(engine, cfg) -> None:
    """Install a loaded config the way _plan_and_run does."""
    engine._current_cfg = cfg
    engine._current_bindings = cfg.bindings
    engine._current_tun = cfg.tunables


# The package every engine module's `_LOGGER = logging.getLogger(__name__)` lands
# under (runner.py -> "...engine.runner", io.py -> "...engine.io", etc).
ENGINE_LOGGER_PREFIX = "custom_components.geodrops_rachio.engine"


def log_trail_native(caplog) -> list:
    """(level, message) pairs the native engine logged via `_LOGGER`.

    Restricted to the engine package's own loggers so caplog picking up an
    unrelated warning (pytest plugins, other components) can't cause a false
    mismatch. Level is lower-cased (`_LOGGER.warning` -> `("warning", msg)`), the
    form the golden fixtures store.
    """
    return [
        (record.levelname.lower(), record.getMessage())
        for record in caplog.records
        if record.name.startswith(ENGINE_LOGGER_PREFIX)
    ]
