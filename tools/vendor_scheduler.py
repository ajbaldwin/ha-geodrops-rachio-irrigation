from __future__ import annotations

import pathlib
import sys

CANONICAL_STOP_TRIGGER = '@state_trigger("input_button.irrigation_stop")'
STOP_ENTITY = "button.geodrops_rachio_stop"

# The canonical app hardcodes its config/state locations under
# apps/irrigation/, but delivery.py drops the vendored files at the top level
# of <config>/pyscript/. Rewrite the constants to match delivery so the running
# script reads the config it was actually handed and writes state where the
# integration expects it.
CANONICAL_CONFIG_PATH = 'CONFIG_PATH = "/config/pyscript/apps/irrigation/config.yaml"'
DELIVERED_CONFIG_PATH = 'CONFIG_PATH = "/config/pyscript/geodrops_rachio_config.yaml"'
CANONICAL_STATE_DIR = 'STATE_DIR = "/config/pyscript/apps/irrigation/state"'
DELIVERED_STATE_DIR = 'STATE_DIR = "/config/pyscript/geodrops_rachio_state"'

# run_active_boolean is now a switch (switch.geodrops_rachio_run_active), not an
# input_boolean. input_boolean.turn_on/turn_off no-op on a switch entity, so
# crash recovery would never re-arm. Rewrite the run_active service call to the
# switch domain. Matched as a single line (the only service.call on
# input_boolean in the canonical app).
CANONICAL_RUN_ACTIVE_CALL = 'service.call("input_boolean", "turn_on" if on else "turn_off",'
DELIVERED_RUN_ACTIVE_CALL = 'service.call("switch", "turn_on" if on else "turn_off",'

# The canonical app registers pyscript services named `irrigation_*` and writes
# `pyscript.irrigation_*` state entities, and coordinates its run task under the
# global key "irrigation_run". The standalone scheduler uses the SAME names, so
# both installed on one HA box would collide (shared service, state, and
# task-uniqueness namespaces). Namespace the vendored copy to `geodrops_rachio_*`
# so this integration coexists with — and is testable alongside — the standalone.
# Only these three collide in a shared namespace; external helpers the user owns
# (e.g. input_boolean.irrigation_standby) and internal @time_trigger functions
# are left untouched.
NAMESPACED_SERVICES = ("run_now", "preview", "stop", "reset", "refresh_runtimes")
CANONICAL_STATE_PREFIX = "pyscript.irrigation_"
DELIVERED_STATE_PREFIX = "pyscript.geodrops_rachio_"
CANONICAL_TASK_KEY = 'task.unique("irrigation_run")'
DELIVERED_TASK_KEY = 'task.unique("geodrops_rachio_run")'


class TransformError(Exception):
    pass


def _require_replace(source: str, expected: str, replacement: str, *, what: str) -> str:
    """Replace ``expected`` with ``replacement``, raising if it is absent.

    Every rewrite the transform performs must fail loudly when the canonical
    source string it targets is missing — a silent no-op would ship a vendored
    script that reads the wrong path or drives the wrong service domain.
    """
    if expected not in source:
        raise TransformError(
            f"expected {what} {expected!r} not found; "
            "the canonical app layout changed — update the transform."
        )
    return source.replace(expected, replacement)


def transform_app_to_script(source: str, *, stop_entity: str) -> str:
    out = _require_replace(
        source,
        CANONICAL_STOP_TRIGGER,
        f'@state_trigger("{stop_entity}")',
        what="stop trigger",
    )
    out = _require_replace(
        out, CANONICAL_CONFIG_PATH, DELIVERED_CONFIG_PATH, what="config path constant"
    )
    out = _require_replace(
        out, CANONICAL_STATE_DIR, DELIVERED_STATE_DIR, what="state dir constant"
    )
    out = _require_replace(
        out,
        CANONICAL_RUN_ACTIVE_CALL,
        DELIVERED_RUN_ACTIVE_CALL,
        what="run_active service call",
    )
    # Namespace the colliding service defs so this integration coexists with
    # the standalone scheduler on the same HA box.
    for name in NAMESPACED_SERVICES:
        out = _require_replace(
            out,
            f"def irrigation_{name}(",
            f"def geodrops_rachio_{name}(",
            what=f"service def irrigation_{name}",
        )
    # Namespace all pyscript.irrigation_* state entities (and any doc references
    # to them) in one shot so reads and writes stay consistent.
    out = _require_replace(
        out,
        CANONICAL_STATE_PREFIX,
        DELIVERED_STATE_PREFIX,
        what="pyscript state entity prefix",
    )
    # Namespace the run-task uniqueness key so our run and the standalone's do
    # not cross-cancel each other.
    out = _require_replace(
        out, CANONICAL_TASK_KEY, DELIVERED_TASK_KEY, what="run task uniqueness key"
    )
    return out


def vendor(scheduler_repo: str, dest_pkg: str) -> None:
    src_root = pathlib.Path(scheduler_repo)
    dest = pathlib.Path(dest_pkg) / "bundled_app"
    app = (src_root / "irrigation/__init__.py").read_text(encoding="utf-8")
    script = transform_app_to_script(app, stop_entity=STOP_ENTITY)
    (dest).mkdir(parents=True, exist_ok=True)
    (dest / "geodrops_rachio.py").write_text(script, encoding="utf-8")
    lib_src = src_root / "irrigation_lib"
    lib_dst = dest / "irrigation_lib"
    if lib_dst.exists():
        import shutil; shutil.rmtree(lib_dst)
    import shutil
    shutil.copytree(lib_src, lib_dst, ignore=shutil.ignore_patterns("__pycache__"))
    version = (src_root / "VERSION").read_text().strip() if (src_root / "VERSION").exists() else "unknown"
    (dest / "VERSION").write_text(version + "\n", encoding="utf-8")
    print(f"vendored scheduler {version} -> {dest}")


if __name__ == "__main__":
    vendor(sys.argv[1], sys.argv[2])
