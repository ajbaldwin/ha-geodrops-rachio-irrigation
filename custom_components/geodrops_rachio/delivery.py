from __future__ import annotations
import hashlib
import pathlib
import shutil
from homeassistant.core import HomeAssistant
from .config_writer import generate_config

CONFIG_FILENAME = "geodrops_rachio_config.yaml"
SCRIPT_FILENAME = "geodrops_rachio.py"
INSTALLED_STAMP = ".geodrops_rachio_version"
# Namespaced lib package name (see tools/vendor_scheduler.py): delivered to the
# shared pyscript modules/ dir without colliding with the standalone
# scheduler's irrigation_lib.
LIB_DIRNAME = "geodrops_rachio_lib"


def read_stamp(path) -> str | None:
    p = pathlib.Path(path)
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def bundle_fingerprint(bundled_dir) -> str:
    """Content hash of the delivered code (script + irrigation_lib).

    Delivery gates on this, not on the VERSION string: two bundles with the
    same VERSION but different code (e.g. a re-vendored script) must still
    redeliver. Ignores __pycache__ so compiled artifacts don't perturb it.
    """
    bundled_dir = pathlib.Path(bundled_dir)
    h = hashlib.sha256()
    h.update((bundled_dir / SCRIPT_FILENAME).read_bytes())
    lib = bundled_dir / LIB_DIRNAME
    for f in sorted(lib.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(str(f.relative_to(lib)).encode("utf-8"))
            h.update(b"\0")
            h.update(f.read_bytes())
    return h.hexdigest()


def needs_delivery(expected: str, installed_stamp_path) -> bool:
    return read_stamp(installed_stamp_path) != expected.strip()


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
    stamp_path = pyscript_dir / INSTALLED_STAMP
    config_path = pyscript_dir / CONFIG_FILENAME

    config_text = generate_config(entry_data)  # raises ValueError on bad overrides

    def _read_delivery_state() -> tuple[str, bool]:
        # Fingerprint (hash of the bundled code) + the installed-stamp read are
        # both blocking file I/O, so this whole comparison runs in the executor
        # job, never on the loop. Gating on content, not the VERSION string,
        # means a re-vendored script always redelivers.
        fingerprint = bundle_fingerprint(bundled_dir)
        return fingerprint, needs_delivery(fingerprint, stamp_path)

    fingerprint, code_changed = await hass.async_add_executor_job(_read_delivery_state)

    def _write_code() -> None:
        (pyscript_dir / "modules").mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_dir / SCRIPT_FILENAME, pyscript_dir / SCRIPT_FILENAME)
        lib_dst = pyscript_dir / "modules" / LIB_DIRNAME
        if lib_dst.exists():
            shutil.rmtree(lib_dst)
        shutil.copytree(bundled_dir / LIB_DIRNAME, lib_dst)
        stamp_path.write_text(fingerprint + "\n", encoding="utf-8")

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
