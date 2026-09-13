import pytest
from tools.vendor_scheduler import transform_app_to_script, TransformError

SAMPLE = '''\
@state_trigger("input_button.irrigation_stop")
def _on_stop_button():
    pass

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
