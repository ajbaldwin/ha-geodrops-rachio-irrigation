from unittest.mock import AsyncMock

from custom_components.geodrops_rachio import delivery


def test_needs_delivery_when_stamp_missing(tmp_path):
    assert delivery.needs_delivery("1.2.3", tmp_path / "VERSION") is True


def test_no_delivery_when_stamp_matches(tmp_path):
    stamp = tmp_path / "VERSION"
    stamp.write_text("1.2.3\n")
    assert delivery.needs_delivery("1.2.3", stamp) is False


def test_needs_delivery_when_stamp_differs(tmp_path):
    stamp = tmp_path / "VERSION"
    stamp.write_text("1.2.2\n")
    assert delivery.needs_delivery("1.2.3", stamp) is True


async def test_async_deliver_writes_files_and_reloads(hass, tmp_path, monkeypatch):
    bundled = tmp_path / "bundled_app"
    (bundled / "irrigation_lib").mkdir(parents=True)
    (bundled / "geodrops_rachio.py").write_text("# script\n")
    (bundled / "irrigation_lib" / "__init__.py").write_text("")
    (bundled / "VERSION").write_text("2.0.0\n")

    pyscript_dir = tmp_path / "pyscript"
    pyscript_dir.mkdir()

    # hass.services is a real ServiceRegistry with __slots__, so a plain
    # instance-attribute assignment (`hass.services.async_call = AsyncMock()`)
    # raises AttributeError. Patch the class attribute instead via
    # monkeypatch (auto-reverted at teardown) — same effect, since Mock
    # objects aren't descriptors and won't get bound like a real method.
    monkeypatch.setattr(type(hass.services), "async_call", AsyncMock())
    entry_data = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
                  "advanced_overrides": ""}

    changed = await delivery.async_deliver(
        hass, entry_data, pyscript_dir=pyscript_dir, bundled_dir=bundled)

    assert changed is True
    assert (pyscript_dir / "geodrops_rachio.py").exists()
    assert (pyscript_dir / "modules" / "irrigation_lib" / "__init__.py").exists()
    assert (pyscript_dir / "geodrops_rachio_config.yaml").exists()
    assert delivery.read_stamp(pyscript_dir / delivery.INSTALLED_STAMP) == "2.0.0"
    hass.services.async_call.assert_awaited_with("pyscript", "reload", blocking=True)

    # Second call is a no-op (stamp matches)
    hass.services.async_call.reset_mock()
    changed2 = await delivery.async_deliver(
        hass, entry_data, pyscript_dir=pyscript_dir, bundled_dir=bundled)
    assert changed2 is False
    hass.services.async_call.assert_not_awaited()


async def test_async_deliver_reloads_when_only_config_changes(hass, tmp_path, monkeypatch):
    bundled = tmp_path / "bundled_app"
    (bundled / "irrigation_lib").mkdir(parents=True)
    (bundled / "geodrops_rachio.py").write_text("# script\n")
    (bundled / "irrigation_lib" / "__init__.py").write_text("")
    (bundled / "VERSION").write_text("2.0.0\n")

    pyscript_dir = tmp_path / "pyscript"
    pyscript_dir.mkdir()

    monkeypatch.setattr(type(hass.services), "async_call", AsyncMock())
    entry_data = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
                  "advanced_overrides": ""}

    changed = await delivery.async_deliver(
        hass, entry_data, pyscript_dir=pyscript_dir, bundled_dir=bundled)
    assert changed is True

    # Same bundled dir/stamp (code unchanged), but different entry_data so
    # the generated config text differs -> must rewrite config and reload,
    # even though the code/version stamp did not change.
    hass.services.async_call.reset_mock()
    new_entry_data = {"bindings": {}, "zones": [], "self_calibration_enabled": True,
                       "advanced_overrides": ""}
    changed2 = await delivery.async_deliver(
        hass, new_entry_data, pyscript_dir=pyscript_dir, bundled_dir=bundled)

    assert changed2 is False
    hass.services.async_call.assert_awaited_with("pyscript", "reload", blocking=True)
    updated_config = (pyscript_dir / "geodrops_rachio_config.yaml").read_text(encoding="utf-8")
    assert "self_calibration_enabled: true" in updated_config
