"""Skip reasons read as words on every human surface, never as internal keys."""
from custom_components.geodrops_rachio.brain import report_format
from custom_components.geodrops_rachio.brain.report_format import RunResult


def _result(**kw):
    base = dict(watered=["front"], uncompleted={"left_wall": "probe_inactive"},
                start="05:42", end="07:06", per_zone_minutes={"front": 12.0},
                standby=False)
    base.update(kw)
    return RunResult(**base)


def test_an_inactive_probe_reads_inactive_in_the_notification():
    text = report_format.format_notification(_result())
    assert "Left Wall (Inactive)" in text
    assert "probe_inactive" not in text


def test_an_inactive_probe_reads_inactive_in_the_calendar():
    _title, body = report_format.format_calendar(_result(), 12, 30)
    assert "- Left Wall: Inactive" in body
    assert "probe_inactive" not in body


def test_an_inactive_probe_reads_inactive_in_the_preview():
    text = report_format.format_preview("Level 1", [("front", 12)],
                                        {"left_wall": "probe_inactive"})
    assert "- Left Wall: Inactive" in text


def test_other_reasons_are_shown_as_given():
    assert report_format.display_reason("insufficient window") == "insufficient window"
    assert report_format.display_reason("excluded") == "excluded"
