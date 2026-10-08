"""教室侧设备选择的持久化。

只存设备**名字**，不存索引——索引会随插拔和重启变化，名字才稳定。开设备时再
解析成索引（见 ``AudioEngine.resolve_device``）。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, ValidationError

from .paths import SETTINGS_PATH

if TYPE_CHECKING:
    from pathlib import Path


class DeviceSelection(BaseModel):
    """教室侧要用的麦克风和音响。``None`` 表示跟随系统默认设备。"""

    in_device: str | None = None
    out_device: str | None = None


class SettingsStore:
    """把选择读写到一个 JSON 文件。文件损坏或不存在一律退回默认值，不阻塞启动。"""

    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path

    def load(self) -> DeviceSelection:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return DeviceSelection()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 {} 失败（{}），按系统默认设备处理", self.path, exc)
            return DeviceSelection()
        try:
            return DeviceSelection.model_validate(raw)
        except ValidationError as exc:
            logger.warning("{} 内容不合法（{}），按系统默认设备处理", self.path, exc)
            return DeviceSelection()

    def save(self, selection: DeviceSelection) -> None:
        """写盘失败只告警：本次仍然生效，只是下次启动记不住。"""
        try:
            self.path.write_text(
                json.dumps(selection.model_dump(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("保存 {} 失败：{}", self.path, exc)
