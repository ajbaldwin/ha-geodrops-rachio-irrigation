"""Automatic watering time: the frost shift, its skips, and the live frost
check at window start (which applies in both timing modes)."""
import datetime as dt

from tests.engine.scenario import ALL_MIXINS, T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld

AUTO = "switch.geodrops_rachio_auto_timing"
MORNING = "sensor.geodrops_rachio_forecast_morning_temperature"
# The scenario's sun: dawn 04:30, sunrise 05:00 (UTC). Level 1 finishes 5 min
# before sunrise, so the anchor-based end is 04:55 and the latest frost
# finish is 06:30.
BASE_END = dt.datetime.fromisoformat("2026-07-02T04:55:00+00:00")
LATEST_END = dt.datetime.fromisoformat("2026-07-02T06:30:00+00:00")


def _utc(hhmm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"2026-07-02T{hhmm}:00+00:00")


def _cold_until(world: FakeWorld, hhmm: str) -> None:
    """The morning forecast: 30 °F before `hhmm`, 50 °F from it."""
    clear = _utc(hhmm)
    hourly = [[_utc(f"{h:02d}:00").isoformat(),
               30.0 if _utc(f"{h:02d}:00") < clear else 50.0] for h in range(12)]
    world.set(MORNING, "30.0", {"hourly": hourly})


def _eng(freezer, *, auto: bool, at: str = T_PLAN, data: dict | None = None):
    freezer.move_to(at)
    world = FakeWorld(freezer)
    data = data or entry_data()
    populate(world, data)
    world.set(AUTO, "on" if auto else "off")
    eng = native_engine(world, data, *ALL_MIXINS)
    cfg = eng._load_cfg()
    eng._current_cfg, eng._current_bindings = cfg, cfg.bindings
    eng._current_tun = cfg.tunables
    return world, eng, cfg


async def test_auto_timing_ignores_the_finish_controls(freezer):
    world, eng, cfg = _eng(freezer, auto=True)
    world.set("select.geodrops_rachio_finish_anchor", "dawn")
    world.set("number.geodrops_rachio_finish_offset", "30")
    profile = cfg.drought_profiles["Level 1 - Mild"]
    assert eng._window_end(cfg, profile) == ("sunrise", 5, BASE_END)


async def test_manual_timing_ignores_the_frost_forecast(freezer):
    world, eng, cfg = _eng(freezer, auto=False)
    _cold_until(world, "06:00")
    ctx = await eng._plan_context(cfg)
    assert ctx["frost"] is None and ctx["timing_mode"] == "manual"
    assert ctx["end"] == BASE_END


async def test_no_cold_morning_changes_nothing(freezer):
    world, eng, cfg = _eng(freezer, auto=True)
    _cold_until(world, "00:00")
    ctx = await eng._plan_context(cfg)
    assert ctx["end"] == BASE_END
    assert ctx["frost"] == {"shift_minutes": 0, "clear_at": None, "skip": False}


async def test_a_cold_morning_moves_the_run_past_the_cold(freezer):
    world, eng, cfg = _eng(freezer, auto=True)
    _cold_until(world, "05:00")
    ctx = await eng._plan_context(cfg)
    span = ctx["the_plan"].span_minutes
    assert ctx["the_plan"].watered
    assert ctx["end"] == min(_utc("05:00") + dt.timedelta(minutes=span), LATEST_END)
    assert ctx["start"] >= _utc("05:00")
    assert ctx["earliest_start"] >= _utc("05:00")
    assert ctx["frost"]["shift_minutes"] == round(
        (ctx["end"] - BASE_END).total_seconds() / 60)
    assert ctx["frost"]["clear_at"] == _utc("05:00").isoformat()


async def test_a_tight_shifted_window_drops_zones(freezer):
    """Cold until 06:00 leaves 30 min before the latest finish: the plan is
    rebuilt there, keeping the one-cycle zone and dropping the other."""
    data = entry_data()
    data["zones"][0]["runtime_minutes"] = 10
    data["zones"][1]["runtime_minutes"] = 60
    world, eng, cfg = _eng(freezer, auto=True, data=data)
    world.set("sensor.front_dominant", "40.0")  # driest first: a fixed order
    _cold_until(world, "06:00")
    ctx = await eng._plan_context(cfg)
    assert ctx["the_plan"].watered == ["front"]
    assert ctx["uncompleted"]["back"] == "insufficient window"
    assert ctx["cap_minutes"] == 30
    assert ctx["start"] == _utc("06:00")
    assert ctx["end"] == _utc("06:00") + dt.timedelta(
        minutes=ctx["the_plan"].span_minutes)


async def test_a_shifted_window_nothing_fits_skips(freezer):
    world, eng, cfg = _eng(freezer, auto=True)
    _cold_until(world, "06:00")
    ctx = await eng._plan_context(cfg)
    assert ctx["frost"]["skip"] is True and ctx["end"] == BASE_END


async def test_run_now_is_not_shifted(freezer):
    world, eng, cfg = _eng(freezer, auto=True)
    _cold_until(world, "06:00")
    ctx = await eng._plan_context(cfg, frost_shift=False)
    assert ctx["frost"] is None and ctx["end"] == BASE_END


async def test_cold_through_the_cap_skips_the_night(freezer):
    world, eng, _cfg = _eng(freezer, auto=True)
    _cold_until(world, "07:00")
    await eng._plan_and_run(True, "nightly")
    value, attrs = eng.records["last_run"]["value"], eng.records["last_run"]["attributes"]
    assert value == 0 and attrs["skipped"] == "frost-forecast"
    assert attrs["frost"]["skip"] is True
    assert set(attrs["uncompleted"].values()) == {"frost-forecast"}
    assert eng.records["last_nightly"]["attributes"]["skipped"] == "frost-forecast"
    recap = [d["message"] for dom, _svc, d in world.calls if dom == "notify"]
    assert recap and "Frost: forecast below 38°F until" in recap[-1]
    assert not any(dom == "rachio" for dom, _svc, _d in world.calls)


async def test_a_shifted_night_waters_after_the_cold(freezer):
    world, eng, _cfg = _eng(freezer, auto=True)
    _cold_until(world, "05:00")
    await eng._plan_and_run(True, "nightly")
    attrs = eng.records["last_run"]["attributes"]
    assert "skipped" not in attrs and attrs["watered"]
    assert attrs["start"] >= "05:00"
    recap = [d["message"] for dom, _svc, d in world.calls if dom == "notify"]
    assert "Frost: finish moved" in recap[-1]


async def test_live_frost_at_window_start_skips_in_manual_mode(freezer):
    world, eng, _cfg = _eng(freezer, auto=False)
    world.at("2026-07-01 23:30:00",
             lambda: world.set("sensor.tempest_sensor_temperature", "31.5"))
    await eng._plan_and_run(True, "nightly")
    attrs = eng.records["last_run"]["attributes"]
    assert attrs["skipped"] == "frost-at-window-start"
    recap = [d["message"] for dom, _svc, d in world.calls if dom == "notify"]
    assert "Frost: 31.5°F at window start (below 34°F)" in recap[-1]
    assert not any(dom == "rachio" for dom, _svc, _d in world.calls)


async def test_unreadable_live_temperature_skips_nothing(freezer):
    world, eng, _cfg = _eng(freezer, auto=False)
    world.at("2026-07-01 23:30:00",
             lambda: world.set("sensor.tempest_sensor_temperature", "unavailable"))
    await eng._plan_and_run(True, "nightly")
    attrs = eng.records["last_run"]["attributes"]
    assert "skipped" not in attrs and attrs["watered"]


async def test_a_replan_past_the_anchor_uses_the_shifted_window(freezer):
    """A restart re-plans at 04:58, after the 04:55 anchor-based end but
    before the cold clears: the run still fits after the cold."""
    world, eng, cfg = _eng(freezer, auto=True, at="2026-07-02 04:58:00")
    _cold_until(world, "05:00")
    ctx = await eng._plan_context(cfg)
    assert ctx["the_plan"].watered
    assert ctx["start"] >= _utc("05:00") and ctx["end"] <= LATEST_END


async def test_a_missing_morning_forecast_shifts_nothing(freezer):
    _world, eng, cfg = _eng(freezer, auto=True)
    ctx = await eng._plan_context(cfg)
    assert ctx["end"] == BASE_END and ctx["frost"]["shift_minutes"] == 0


async def test_projection_shows_the_frost_shift(freezer):
    world, eng, _cfg = _eng(freezer, auto=True)
    _cold_until(world, "05:00")
    eng.records["preview"] = {"value": 2, "attributes": {"span_minutes": 60}}
    w = eng.project_window()
    assert w["timing_mode"] == "auto"
    assert w["end"] == _utc("06:00") and w["start"] == _utc("05:00")
    assert w["frost"] == {"shift_minutes": 65, "clear_at": _utc("05:00"),
                          "skip": False}


async def test_projection_without_auto_timing_has_no_frost(freezer):
    world, eng, _cfg = _eng(freezer, auto=False)
    _cold_until(world, "05:00")
    w = eng.project_window()
    assert w["timing_mode"] == "manual" and w["frost"] is None
    assert w["end"] == BASE_END
