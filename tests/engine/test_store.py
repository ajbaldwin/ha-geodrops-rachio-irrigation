import json

from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.geodrops_rachio.engine import store as es


async def _nosave(_docs):
    return None


async def test_engine_store_reads_are_copies_and_writes_save():
    saved = []

    async def save(docs):
        saved.append(docs)

    s = es.EngineStore({"efficacy": {"a": {"state": "calibrating"}}}, save)
    got = s.read(es.EFFICACY)
    got["a"]["state"] = "mutated"
    assert s.read(es.EFFICACY)["a"]["state"] == "calibrating"
    assert s.read(es.PENDING_OBS) is None
    await s.write(es.PENDING_OBS, [1])
    assert saved[-1]["pending_obs"] == [1]
    await s.delete(es.PENDING_OBS)
    assert s.read(es.PENDING_OBS) is None


async def test_on_write_callback_fires():
    s = es.EngineStore({}, _nosave)
    hits = []
    s.on_write = lambda: hits.append(1)
    await s.write(es.EFFICACY, {})
    assert hits == [1]


def _seed_legacy(config_dir):
    ps = config_dir / "pyscript"
    (ps / "modules" / "geodrops_rachio_lib").mkdir(parents=True)
    (ps / "modules" / "geodrops_rachio_lib" / "plan.py").write_text("x=1")
    (ps / "geodrops_rachio.py").write_text("# legacy")
    (ps / "geodrops_rachio_config.yaml").write_text("a: 1")
    (ps / ".geodrops_rachio_version").write_text("abc")
    state = ps / "geodrops_rachio_state"
    state.mkdir()
    (state / "irrigation_efficacy.json").write_text(json.dumps({"front": {"efficacy": 0.4}}))
    (state / "geodrops_rachio_last_nightly.json").write_text(
        json.dumps({"value": 2, "attributes": {"watered": ["front"]}}))
    return ps


async def test_open_store_imports_legacy_and_retires_pyscript(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)
    reloads = async_mock_service(hass, "pyscript", "reload")

    s = await es.async_open_store(hass, "entry1")

    assert s.read(es.EFFICACY) == {"front": {"efficacy": 0.4}}
    assert s.read(es.record_key("last_nightly"))["value"] == 2
    assert not (ps / "geodrops_rachio.py").exists()
    assert not (ps / "geodrops_rachio_config.yaml").exists()
    assert not (ps / ".geodrops_rachio_version").exists()
    assert not (ps / "modules" / "geodrops_rachio_lib").exists()
    assert (ps / "geodrops_rachio_state" / "irrigation_efficacy.json").exists()
    assert len(reloads) == 1
    stored = await Store(hass, es.STORAGE_VERSION, "geodrops_rachio.entry1").async_load()
    assert stored["docs"]["efficacy"] == {"front": {"efficacy": 0.4}}


async def test_open_store_second_time_keeps_store(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)
    async_mock_service(hass, "pyscript", "reload")
    s1 = await es.async_open_store(hass, "entry1")
    await s1.write(es.EFFICACY, {"front": {"efficacy": 0.9}})
    # Legacy file changes, but nothing was re-delivered: must NOT re-import.
    (ps / "geodrops_rachio_state" / "irrigation_efficacy.json").write_text("{}")
    reloads = async_mock_service(hass, "pyscript", "reload")
    s2 = await es.async_open_store(hass, "entry1")
    assert s2.read(es.EFFICACY) == {"front": {"efficacy": 0.9}}
    assert reloads == []


async def test_open_store_reimports_after_rollback_roundtrip(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)
    async_mock_service(hass, "pyscript", "reload")
    await es.async_open_store(hass, "entry1")
    # Rollback to v0.9.15 re-delivers the script and pyscript learns more.
    (ps / "geodrops_rachio.py").write_text("# legacy again")
    (ps / "geodrops_rachio_state" / "irrigation_efficacy.json").write_text(
        json.dumps({"front": {"efficacy": 0.7}}))
    s = await es.async_open_store(hass, "entry1")
    assert s.read(es.EFFICACY) == {"front": {"efficacy": 0.7}}


async def test_open_store_fresh_install_without_pyscript(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    s = await es.async_open_store(hass, "entry1")   # no pyscript dir, no service
    assert s.read(es.EFFICACY) is None


async def test_open_store_survives_failing_pyscript_reload(hass, tmp_path, caplog):
    hass.config.config_dir = str(tmp_path)
    ps = _seed_legacy(tmp_path)

    async def _boom(_call):
        raise RuntimeError("pyscript reload exploded")
    hass.services.async_register("pyscript", "reload", _boom)

    s = await es.async_open_store(hass, "entry1")

    assert s.read(es.EFFICACY) == {"front": {"efficacy": 0.4}}
    assert not (ps / "geodrops_rachio.py").exists()
    assert not (ps / "modules" / "geodrops_rachio_lib").exists()
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("pyscript.reload failed" in m and "exploded" in m for m in msgs)
    assert any("pyscript reloaded: False" in m for m in msgs)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
