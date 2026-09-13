import pytest
from tools.vendor_scheduler import transform_app_to_script, TransformError

# A sample that mirrors the canonical app's exact constant/service-call strings
# for the pieces the transform rewrites (stop trigger, config/state paths, and
# the run_active service call).
SAMPLE = '''\
@state_trigger("input_button.irrigation_stop")
def _on_stop_button():
    pass

CONFIG_PATH = "/config/pyscript/apps/irrigation/config.yaml"
STATE_DIR = "/config/pyscript/apps/irrigation/state"

STATUS_ENTITY = "pyscript.irrigation_status"


def set_run_active(on):
    service.call("input_boolean", "turn_on" if on else "turn_off",
                 entity_id=_current_bindings.run_active_boolean)


@state_trigger("input_boolean.irrigation_standby")
def _on_standby():
    pass


@time_trigger("cron(0 23 * * *)")
def irrigation_nightly():
    task.unique("irrigation_run")
    state.set("pyscript.irrigation_last_nightly", value="{}")


@time_trigger("cron(0 6 * * *)")
def irrigation_calibrate():
    pass


@service
def irrigation_run_now():
    task.unique("irrigation_run")
    state.set("pyscript.irrigation_status", value="running")


@service
def irrigation_preview():
    state.set("pyscript.irrigation_preview", value="standby")


@service
def irrigation_stop():
    pass


@service
def irrigation_reset():
    pass


@service
def irrigation_refresh_runtimes():
    state.set("pyscript.irrigation_runtimes", value="{}")
'''


def test_rewrites_stop_entity():
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    assert '@state_trigger("button.geodrops_rachio_stop")' in out
    assert "input_button.irrigation_stop" not in out
    assert 'cron(0 23 * * *)' in out  # schedules untouched


def test_missing_stop_trigger_raises():
    with pytest.raises(TransformError):
        transform_app_to_script("def nothing():\n    pass\n",
                                stop_entity="button.geodrops_rachio_stop")


def test_rewrites_config_and_state_paths():
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    # Config path rewritten to the top-level delivery location.
    assert 'CONFIG_PATH = "/config/pyscript/geodrops_rachio_config.yaml"' in out
    # State dir rewritten to a top-level location.
    assert 'STATE_DIR = "/config/pyscript/geodrops_rachio_state"' in out
    # The canonical apps/irrigation paths must be gone entirely.
    assert "/config/pyscript/apps/irrigation" not in out


def test_missing_config_path_raises():
    src = SAMPLE.replace(
        'CONFIG_PATH = "/config/pyscript/apps/irrigation/config.yaml"',
        'CONFIG_PATH = "/somewhere/else/config.yaml"',
    )
    with pytest.raises(TransformError):
        transform_app_to_script(src, stop_entity="button.geodrops_rachio_stop")


def test_missing_state_dir_raises():
    src = SAMPLE.replace(
        'STATE_DIR = "/config/pyscript/apps/irrigation/state"',
        'STATE_DIR = "/somewhere/else/state"',
    )
    with pytest.raises(TransformError):
        transform_app_to_script(src, stop_entity="button.geodrops_rachio_stop")


def test_rewrites_run_active_to_switch_domain():
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    # run_active is a switch entity now, so the marker must be driven via the
    # switch domain, not input_boolean (which would no-op on a switch).
    assert 'service.call("switch", "turn_on" if on else "turn_off",' in out
    assert 'entity_id=_current_bindings.run_active_boolean)' in out
    assert 'service.call("input_boolean"' not in out


def test_missing_run_active_call_raises():
    src = SAMPLE.replace(
        'service.call("input_boolean", "turn_on" if on else "turn_off",',
        'service.call("input_boolean", "toggle",',
    )
    with pytest.raises(TransformError):
        transform_app_to_script(src, stop_entity="button.geodrops_rachio_stop")


# --- Service-name / state / task-key namespacing (coexistence with the
# standalone scheduler on the same HA box) -----------------------------------

def test_namespaces_service_defs():
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    for name in ("run_now", "preview", "stop", "reset", "refresh_runtimes"):
        assert f"def geodrops_rachio_{name}(" in out
        assert f"def irrigation_{name}(" not in out


def test_namespaces_pyscript_state_entities():
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    for suffix in ("status", "preview", "last_nightly", "runtimes"):
        assert f"pyscript.geodrops_rachio_{suffix}" in out
    # No colliding pyscript.irrigation_* entity may remain anywhere.
    assert "pyscript.irrigation_" not in out


def test_namespaces_task_unique_key():
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    assert 'task.unique("geodrops_rachio_run")' in out
    assert 'task.unique("irrigation_run")' not in out


def test_preserves_external_and_trigger_names():
    """External helpers and internal @time_trigger functions are NOT renamed."""
    out = transform_app_to_script(SAMPLE, stop_entity="button.geodrops_rachio_stop")
    # External helper the user owns — reading it is not an ownership collision.
    assert "input_boolean.irrigation_standby" in out
    # Non-service trigger functions register no service, so they don't collide.
    assert "def irrigation_nightly(" in out
    assert "def irrigation_calibrate(" in out


def test_missing_service_def_raises():
    src = SAMPLE.replace("def irrigation_run_now():", "def irrigation_go():")
    with pytest.raises(TransformError):
        transform_app_to_script(src, stop_entity="button.geodrops_rachio_stop")


def test_namespaces_lib_import_package():
    src = SAMPLE + "\nimport irrigation_lib.config as config\n"
    out = transform_app_to_script(src, stop_entity="button.geodrops_rachio_stop")
    assert "import geodrops_rachio_lib.config as config" in out
    # The shared package name must be gone so we never clobber the standalone
    # scheduler's /config/pyscript/modules/irrigation_lib.
    assert "irrigation_lib" not in out
