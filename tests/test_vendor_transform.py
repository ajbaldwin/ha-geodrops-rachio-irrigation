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


def set_run_active(on):
    service.call("input_boolean", "turn_on" if on else "turn_off",
                 entity_id=_current_bindings.run_active_boolean)


@time_trigger("cron(0 23 * * *)")
def irrigation_nightly():
    pass
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
