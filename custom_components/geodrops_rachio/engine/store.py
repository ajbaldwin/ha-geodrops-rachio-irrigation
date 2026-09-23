"""Engine persistence: named JSON documents in one HA Store per config entry.

The pyscript app kept its restart-surviving state as JSON files in
/config/pyscript/geodrops_rachio_state/. Each file becomes one document here,
so the ported code reads/writes exactly what it used to (see LEGACY_FILE_KEYS).
Reads return deep copies, preserving the app's read-fresh-from-file semantics.

Setup also retires the pyscript delivery: it deletes the files v0.9.x copied
into /config/pyscript/ and, if it deleted any, reloads pyscript so a legacy
script already loaded this boot cannot also water tonight.
"""
from __future__ import annotations

import copy
import json
import logging
import pathlib
import shutil
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
EFFICACY = "efficacy"
PENDING_OBS = "pending_obs"
WAITING_MARKER = "waiting_marker"
PERSISTED_RECORDS = ("last_nightly", "calibration", "targets", "preview")
LEGACY_STATE_DIRNAME = "geodrops_rachio_state"
DELIVERED_PATHS = (
    "geodrops_rachio.py",
    "geodrops_rachio_config.yaml",
    ".geodrops_rachio_version",
    "modules/geodrops_rachio_lib",
)


def record_key(name: str) -> str:
    return f"record.{name}"


LEGACY_FILE_KEYS = {
    "irrigation_efficacy.json": EFFICACY,
    "irrigation_pending_obs.json": PENDING_OBS,
    "irrigation_waiting.json": WAITING_MARKER,
    **{f"geodrops_rachio_{n}.json": record_key(n) for n in PERSISTED_RECORDS},
}


class EngineStore:
    def __init__(self, docs: dict, save: Callable[[dict], Awaitable[None]]) -> None:
        self._docs = copy.deepcopy(dict(docs or {}))
        self._save = save
        self.on_write: Callable[[], None] | None = None

    def read(self, key: str) -> Any | None:
        return copy.deepcopy(self._docs.get(key))

    async def write(self, key: str, value: Any) -> None:
        self._docs[key] = copy.deepcopy(value)
        await self._flush()

    async def delete(self, key: str) -> None:
        if self._docs.pop(key, None) is not None:
            await self._flush()

    async def _flush(self) -> None:
        await self._save(copy.deepcopy(self._docs))
        if self.on_write is not None:
            self.on_write()


def read_legacy_state(state_dir: pathlib.Path) -> dict:
    docs: dict = {}
    for fname, key in LEGACY_FILE_KEYS.items():
        path = state_dir / fname
        try:
            docs[key] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as err:
            _LOGGER.warning("geodrops_rachio: could not import legacy %s (%s)", fname, err)
    return docs


def remove_delivered(pyscript_dir: pathlib.Path) -> list[str]:
    removed: list[str] = []
    for rel in DELIVERED_PATHS:
        path = pyscript_dir / rel
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(rel)
        elif path.exists():
            path.unlink()
            removed.append(rel)
    return removed


async def async_open_store(hass: HomeAssistant, entry_id: str) -> EngineStore:
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
    data = await store.async_load()
    pyscript_dir = pathlib.Path(hass.config.path("pyscript"))
    removed = await hass.async_add_executor_job(remove_delivered, pyscript_dir)
    reloaded = False
    if removed and hass.services.has_service("pyscript", "reload"):
        await hass.services.async_call("pyscript", "reload", blocking=True)
        reloaded = True
    if data is None or removed:
        docs = await hass.async_add_executor_job(
            read_legacy_state, pyscript_dir / LEGACY_STATE_DIRNAME)
        data = {"docs": docs}
        await store.async_save(data)
        if docs or removed:
            _LOGGER.warning(
                "geodrops_rachio: native engine took over — imported %d legacy "
                "document(s) (%d zone(s) of calibration history); removed %s; "
                "pyscript reloaded: %s",
                len(docs), len(docs.get(EFFICACY) or {}),
                ", ".join(removed) or "nothing", reloaded)

    async def _save(docs: dict) -> None:
        await store.async_save({"docs": docs})

    return EngineStore(data.get("docs", {}), _save)
