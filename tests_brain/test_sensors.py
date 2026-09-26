from brain import config, sensors

ZONE = config.ZoneConfig(
    key="zone_a", rachio_switch="switch.zone_a",
    dominant_sensor="sensor.d", state_sensor="sensor.s",
    quality_sensors=("q1", "q2", "q3"), target_range="moist",
    geography="front", adjacency=(),
)


def sig(dominant="55.0", state="Dry", qualities=("Good", "Good", "Good")):
    return sensors.ZoneSignals(dominant=dominant, state=state, qualities=qualities)


def test_online_when_data_good():
    r = sensors.read_zone(ZONE, sig())
    assert r.online is True
    assert r.offline_reason is None
    assert r.dominant == 55.0
    assert r.index_rank == 0
    assert r.state_text == "Dry"


def test_offline_when_dominant_unavailable():
    r = sensors.read_zone(ZONE, sig(dominant="unavailable"))
    assert r.online is False
    assert r.offline_reason == "unavailable"


def test_offline_when_state_unknown():
    r = sensors.read_zone(ZONE, sig(state="unknown"))
    assert r.online is False
    assert r.offline_reason == "unavailable"


def test_one_poor_depth_stays_online():
    r = sensors.read_zone(ZONE, sig(qualities=("Good", "Poor", "Good")))
    assert r.online is True


def test_all_poor_stays_online():
    # Poor counts as usable — only Bad/Training/Unknown disqualify a depth.
    r = sensors.read_zone(ZONE, sig(qualities=("Poor", "Poor", "Poor")))
    assert r.online is True


def test_good_poor_bad_stays_online():
    # Two usable depths (Good + Poor) clears the majority-usable gate.
    r = sensors.read_zone(ZONE, sig(qualities=("Good", "Poor", "Bad")))
    assert r.online is True


def test_offline_when_two_depths_unusable():
    r = sensors.read_zone(ZONE, sig(qualities=("Poor", "Bad", "Bad")))
    assert r.online is False
    assert r.offline_reason == "low_quality"


def test_offline_when_training_and_unknown():
    r = sensors.read_zone(ZONE, sig(qualities=("Good", "Training", "Unknown")))
    assert r.online is False
    assert r.offline_reason == "low_quality"


def test_offline_when_dominant_not_numeric():
    r = sensors.read_zone(ZONE, sig(dominant="n/a"))
    assert r.online is False
    assert r.offline_reason == "unavailable"


def test_translation_key_states_and_qualities_read_the_same():
    """GeoDrops PR #12: `good`/`poor`/`training` and `moist_plus` instead of the
    labels. Before this, every zone read `low_quality` and nothing watered."""
    old = sensors.read_zone(ZONE, sig(state="Moist+", qualities=("Good", "Poor", "Good")))
    new = sensors.read_zone(ZONE, sig(state="moist_plus", qualities=("good", "poor", "good")))
    assert new.online is True and new.index_rank == old.index_rank == 3


def test_translation_key_bad_and_training_depths_are_unusable():
    r = sensors.read_zone(ZONE, sig(qualities=("good", "training", "bad")))
    assert r.online is False and r.offline_reason == "low_quality"


def test_all_training_recognises_both_forms():
    assert sensors.all_training(("Training", "Training", "Training"))
    assert sensors.all_training(("training", " training", "training"))
    assert not sensors.all_training(("training", "good", "training"))
    assert not sensors.all_training(("training", "training"))
