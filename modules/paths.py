"""运行期目录解析：程序资源 vs 用户数据。

这两类文件打包后**不在一个地方**，混着用会踩坑：

* **资源**（``web/`` 里的静态页面）随程序一起分发、只读，用 ``__file__``
  相对定位就够了——PyInstaller 会把打包进去的模块的 ``__file__`` 设成解包
  目录下的绝对路径，所以 ``Path(__file__).parent.parent / "web"`` 在打包后
  依然正确（onedir 下解析结果是 ``_internal/web``，spec 里 data 的 dest 与
  之对齐即可，见 ``packaging/CallClassroom.spec``）。

* **用户数据**（``settings.json``、自签证书、日志）必须可写且要跨重启保留。
  打包后程序装在 ``%LOCALAPPDATA%\\Programs\\CallClassroom``，跟着程序走的话
  升级/卸载会把用户的选择一起删掉，所以统一落到
  ``%LOCALAPPDATA%\\CallClassroom``，与安装目录分开。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "CallClassroom"


def _data_dir() -> Path:
    """用户数据目录：打包后是 ``%LOCALAPPDATA%\\CallClassroom``，源码运行是仓库根。"""
    if getattr(sys, "frozen", False):
        # 拿不到 LOCALAPPDATA 就退回主目录下的隐藏目录，至少还能跑
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME.lower()}"
    else:
        # 源码运行：仍放仓库根，settings.json 与 .certs/ 都已在 .gitignore 里
        root = Path(__file__).resolve().parent.parent
    root.mkdir(parents=True, exist_ok=True)
    return root


DATA_DIR = _data_dir()

#: 教室侧设备选择
SETTINGS_PATH = DATA_DIR / "settings.json"

#: 自签证书
CERT_DIR = DATA_DIR / ".certs"

#: 打包后是窗口程序、没有终端，日志只能落盘（源码运行时不写这个文件）
LOG_PATH = DATA_DIR / "callclassroom.log"
