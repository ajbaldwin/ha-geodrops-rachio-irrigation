import asyncio
import datetime as dt

from tests.engine.world import FakePort, FakeWorld


def _world(freezer):
    freezer.move_to("2026-07-02 02:00:00")
    w = FakeWorld(freezer)
    w.rachio.zone_switches |= {"switch.a", "switch.b"}
    return w


def test_schedule_walks_zones_in_order(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a", "switch.b"], "duration": "10,5"})
    assert w.get("switch.a") == "on" and w.get("switch.b") == "off"
    w.advance(10 * 60 + 1)
    assert w.get("switch.a") == "off" and w.get("switch.b") == "on"
    w.advance(5 * 60)
    assert w.get("switch.b") == "off"


def test_pause_holds_then_auto_resumes(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    w.advance(60)
    w.call("rachio", "pause_watering", {"devices": "Main", "duration": 2})
    assert w.get("switch.a") == "off"
    w.advance(121)
    assert w.get("switch.a") == "on"          # auto-resumed after 2 min
    w.advance(9 * 60)                         # 1 + 9 = 10 min watered
    assert w.get("switch.a") == "off"


def test_explicit_resume_and_stop(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    w.call("rachio", "pause_watering", {"devices": "Main", "duration": 60})
    w.advance(30)
    w.call("rachio", "resume_watering", {"devices": "Main"})
    assert w.get("switch.a") == "on"
    w.call("rachio", "stop_watering", {"devices": "Main"})
    assert w.get("switch.a") == "off"


def test_zone_turn_off_advances_to_next_zone(freezer):
    w = _world(freezer)
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a", "switch.b"], "duration": "10,5"})
    w.call("switch", "turn_off", {"entity_id": "switch.a"})
    assert w.get("switch.b") == "on"


def test_drop_and_refused_start(freezer):
    w = _world(freezer)
    w.rachio.refuse_next_start = True
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    assert w.get("switch.a") == "off"
    w.call("rachio", "start_multiple_zone_schedule",
           {"entity_id": ["switch.a"], "duration": "10"})
    w.rachio.drop_at = w.now() + dt.timedelta(minutes=3)
    w.advance(179)
    assert w.get("switch.a") == "on"
    w.advance(2)
    assert w.get("switch.a") == "off"


def test_events_fire_in_order_and_return_coroutines(freezer):
    w = _world(freezer)
    seen = []

    async def later():
        seen.append("coro")

    w.at("2026-07-02 02:05:00", lambda: seen.append("sync"))
    w.at("2026-07-02 02:03:00", later)
    pending = w.advance(600)
    assert seen == ["sync"] and len(pending) == 1
    asyncio.run(pending[0])
    assert seen == ["sync", "coro"]


def test_plain_switch_service_updates_state_and_failing(freezer):
    w = _world(freezer)
    w.call("switch", "turn_on", {"entity_id": "switch.run_active"})
    assert w.get("switch.run_active") == "on"
    w.failing.add(("notify", "phone"))
    try:
        w.call("notify", "phone", {"message": "x"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected failure")


async def test_fake_port_sleep_awaits_event_coroutines(freezer):
    w = _world(freezer)
    seen = []

    async def ev():
        seen.append(w.now().strftime("%H:%M"))

    w.at("2026-07-02 02:01:00", ev)
    await FakePort(w).sleep(120)
    assert seen == ["02:02"]  # awaited after the clock reached the target
