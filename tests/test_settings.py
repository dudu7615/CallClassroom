"""设备选择持久化的测试。"""

from __future__ import annotations

import json
from pathlib import Path

from modules.settings import DeviceSelection, SettingsStore


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    selection = SettingsStore(tmp_path / "nope.json").load()
    assert selection.in_device is None
    assert selection.out_device is None


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(DeviceSelection(in_device="USB 麦克风", out_device="教室功放"))

    loaded = store.load()
    assert loaded.in_device == "USB 麦克风"
    assert loaded.out_device == "教室功放"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"in_device": "USB 麦克风", "out_device": "教室功放"}


def test_corrupt_json_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert SettingsStore(path).load() == DeviceSelection()


def test_wrong_shape_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"in_device": ["不是字符串"]}), encoding="utf-8")
    assert SettingsStore(path).load() == DeviceSelection()


def test_null_means_system_default(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(DeviceSelection(out_device="教室功放"))

    loaded = store.load()
    assert loaded.in_device is None, "没选就是跟随系统默认"
    assert loaded.out_device == "教室功放"


def test_save_failure_only_warns(tmp_path: Path) -> None:
    """写盘失败不该抛异常：本次运行仍然生效，只是下次启动记不住。"""
    store = SettingsStore(tmp_path / "no-such-dir" / "settings.json")
    store.save(DeviceSelection(in_device="X"))
