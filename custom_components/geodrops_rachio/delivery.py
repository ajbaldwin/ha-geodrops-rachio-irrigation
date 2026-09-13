from __future__ import annotations
import pathlib
import shutil
from homeassistant.core import HomeAssistant
from .config_writer import generate_config

CONFIG_FILENAME = "geodrops_rachio_config.yaml"
SCRIPT_FILENAME = "geodrops_rachio.py"
INSTALLED_STAMP = ".geodrops_rachio_version"


def read_stamp(path) -> str | None:
    p = pathlib.Path(path)
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def needs_delivery(bundled_version: str, installed_stamp_path) -> bool:
    return read_stamp(installed_stamp_path) != bundled_version.strip()


async def async_deliver(hass: HomeAssistant, entry_data: dict, *,
                         pyscript_dir, bundled_dir) -> bool:
    """Deliver the bundled scheduler script + lib + generated config.

    Copies the version-stamped script and irrigation_lib into
    ``pyscript_dir`` and writes the generated config whenever the bundled
    version differs from what's stamped on disk. Reloads pyscript only when
    something on disk actually changed (code or config) — a pure re-setup
    with an identical bundle and identical config is a no-op.

    Returns True iff the code files (script + lib) were (re)written.
    """
    pyscript_dir = pathlib.Path(pyscript_dir)
    bundled_dir = pathlib.Path(bundled_dir)
    bundled_version = read_stamp(bundled_dir / "VERSION") or "unknown"
    stamp_path = pyscript_dir / INSTALLED_STAMP
    config_path = pyscript_dir / CONFIG_FILENAME

    config_text = generate_config(entry_data)  # raises ValueError on bad overrides

    code_changed = needs_delivery(bundled_version, stamp_path)

    def _write_code() -> None:
        (pyscript_dir / "modules").mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_dir / SCRIPT_FILENAME, pyscript_dir / SCRIPT_FILENAME)
        lib_dst = pyscript_dir / "modules" / "irrigation_lib"
        if lib_dst.exists():
            shutil.rmtree(lib_dst)
        shutil.copytree(bundled_dir / "irrigation_lib", lib_dst)
        stamp_path.write_text(bundled_version + "\n", encoding="utf-8")

    def _write_config() -> None:
        config_path.write_text(config_text, encoding="utf-8")

    def _existing_config() -> str | None:
        return config_path.read_text(encoding="utf-8") if config_path.exists() else None

    if code_changed:
        await hass.async_add_executor_job(_write_code)
        await hass.async_add_executor_job(_write_config)
        await hass.services.async_call("pyscript", "reload", blocking=True)
    else:
        old_config = await hass.async_add_executor_job(_existing_config)
        if old_config != config_text:
            await hass.async_add_executor_job(_write_config)
            await hass.services.async_call("pyscript", "reload", blocking=True)

    return code_changed
