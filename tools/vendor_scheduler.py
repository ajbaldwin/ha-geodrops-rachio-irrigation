from __future__ import annotations

import pathlib
import sys

CANONICAL_STOP_TRIGGER = '@state_trigger("input_button.irrigation_stop")'
STOP_ENTITY = "button.geodrops_rachio_stop"


class TransformError(Exception):
    pass


def transform_app_to_script(source: str, *, stop_entity: str) -> str:
    if CANONICAL_STOP_TRIGGER not in source:
        raise TransformError(
            f"expected stop trigger {CANONICAL_STOP_TRIGGER!r} not found; "
            "the canonical app layout changed — update the transform."
        )
    return source.replace(
        CANONICAL_STOP_TRIGGER, f'@state_trigger("{stop_entity}")'
    )


def vendor(scheduler_repo: str, dest_pkg: str) -> None:
    src_root = pathlib.Path(scheduler_repo)
    dest = pathlib.Path(dest_pkg) / "bundled_app"
    app = (src_root / "pyscript/apps/irrigation/__init__.py").read_text(encoding="utf-8")
    script = transform_app_to_script(app, stop_entity=STOP_ENTITY)
    (dest).mkdir(parents=True, exist_ok=True)
    (dest / "geodrops_rachio.py").write_text(script, encoding="utf-8")
    lib_src = src_root / "pyscript/modules/irrigation_lib"
    lib_dst = dest / "irrigation_lib"
    if lib_dst.exists():
        import shutil; shutil.rmtree(lib_dst)
    import shutil; shutil.copytree(lib_src, lib_dst)
    version = (src_root / "VERSION").read_text().strip() if (src_root / "VERSION").exists() else "unknown"
    (dest / "VERSION").write_text(version + "\n", encoding="utf-8")
    print(f"vendored scheduler {version} -> {dest}")


if __name__ == "__main__":
    vendor(sys.argv[1], sys.argv[2])
