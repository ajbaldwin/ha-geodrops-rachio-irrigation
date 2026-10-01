from custom_components.geodrops_rachio.util import slug, titleize


def test_slug_collapses_runs_and_trims():
    assert slug("  Front -- Slope! ") == "front_slope"
    assert slug("Back Yard") == "back_yard"


def test_titleize_falls_back_to_the_key():
    assert titleize("front_slope") == "Front Slope"
    assert titleize("__") == "__"
