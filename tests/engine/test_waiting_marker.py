"""The waiting marker a parked nightly writes is exactly what startup recovery
needs to re-arm it (the v0.9.x restart bug lived in this hand-off)."""
import asyncio
import datetime as dt

from custom_components.geodrops_rachio.brain import recovery
from custom_components.geodrops_rachio.engine.store import WAITING_MARKER
from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld


async def test_parked_nightly_marker_round_trips_through_recovery(freezer):
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)

    async def parked(_s):
        await asyncio.Event().wait()
    eng.port.sleep = parked
    await eng.irrigation_nightly()
    for _ in range(5):                 # let the run task reach its wait
        await asyncio.sleep(0)
    assert eng.records["status"]["value"] == "waiting"

    marker = eng.store.read(WAITING_MARKER)
    assert set(marker) == {"window_end", "stamp", "trigger"}
    assert marker["stamp"] == "2026-07-01T23:00:00"
    assert marker["trigger"] == "nightly"
    window_end = dt.datetime.fromisoformat(marker["window_end"])
    assert window_end.tzinfo is not None           # aware, as the sun sensor is
    assert window_end > eng.port.now()

    # A restart before the window closes re-arms; one after it records a miss.
    before = (window_end - dt.timedelta(minutes=30)).isoformat()
    after = (window_end + dt.timedelta(minutes=1)).isoformat()
    assert recovery.startup_action(marker, before) == recovery.RE_ARM
    assert recovery.startup_action(marker, after) == recovery.MISSED
    await eng.async_shutdown()


async def test_marker_storage_failures_never_take_down_the_run(freezer, caplog):
    """The marker only buys restart recovery; a failing store must cost that
    and nothing else — the nightly still plans, waits and reports."""
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    real_write, real_delete = eng.store.write, eng.store.delete

    async def write(key, value):
        if key == WAITING_MARKER:
            raise OSError("disk full")
        await real_write(key, value)

    async def delete(key):
        if key == WAITING_MARKER:
            raise OSError("disk full")
        await real_delete(key)

    def read(_key):
        raise ValueError("corrupt")
    eng.store.write, eng.store.delete = write, delete
    await eng.irrigation_nightly()
    await eng.run_task
    assert eng.records["last_nightly"]["attributes"]["trigger"] == "nightly"
    eng.store.read = read
    assert eng._read_waiting_marker() is None
    msgs = [r.getMessage() for r in caplog.records]
    for text in ("could not persist waiting marker (disk full)",
                 "could not clear waiting marker (disk full)",
                 "could not read waiting marker (corrupt)"):
        assert any(text in m for m in msgs), text
