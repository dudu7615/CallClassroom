# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows，onedir）。

在 **Windows** 上跑（PyInstaller 不支持交叉编译，别在 Linux 上试）：

    uv sync --group build
    uv run pyinstaller packaging/CallClassroom.spec --noconfirm --clean

产物是 dist/CallClassroom/ —— 整个目录才是可运行单元，再交给
packaging/setup.iss 封成一个安装包。

为什么是 onedir 而不是 onefile：onefile 每次启动都要把全部内容解包到临时
目录，而这个程序是开机自启的，每次登录都解包几百 MB 不能接受；而且解包目录
退出即删，用户数据写进去会丢。

打包后的目录结构（PyInstaller 6 起非 exe 文件都在 _internal/ 下）::

    dist/CallClassroom/
        CallClassroom.exe
        _internal/
            modules/      <- server.py 在这里，__file__ 往上两层就是 _internal/
            web/          <- 静态页面，必须落在这个位置（见 WEB_DIR 的注释）
            _sounddevice_data/portaudio-binaries/libportaudio64bit.dll

本文件扩展名是 .spec，不在 ruff / basedpyright 的检查范围内。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

#: spec 在 packaging/ 下，仓库根是它的上一层。SPECPATH 是 PyInstaller 注入的
#: spec 全局变量（= 本文件所在目录）。这里一律用绝对路径，免得纠结 datas 里的
#: 相对路径到底是按 spec 位置还是按当前工作目录解析。
ROOT = Path(SPECPATH).parent  # noqa: F821  # SPECPATH 由 PyInstaller 注入

datas = [
    # 静态页面。dest 必须是 "web" —— server.py 的 WEB_DIR 在打包后解析成
    # _internal/web，对不上就是 404。
    (str(ROOT / "web"), "web"),
    # sounddevice 要到运行时才去 _sounddevice_data/portaudio-binaries/ 里加载
    # PortAudio DLL，静态分析看不见这个包（它不在 sounddevice 包里，是个独立的
    # 顶层包）。漏收的话程序不会崩，只会静默降级成"无声卡模式"——最难查的故障。
    *collect_data_files("_sounddevice_data"),
]

hiddenimports = [
    # uvicorn 按名字动态导入它的协议实现（我们用的是 websockets 那条路），
    # 静态分析同样看不见。
    *collect_submodules("uvicorn"),
    *collect_submodules("websockets"),
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CallClassroom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压过的 DLL 在 numpy / cryptography 上出过问题，收益也不大，关掉
    upx=False,
    # 这是后台服务，界面在浏览器里。console=True 会让开机自启时多出一个黑框，
    # 而且用户没法关掉它——日志改走 %LOCALAPPDATA%\CallClassroom\callclassroom.log。
    # 排查问题想直接看输出的话，把这里改成 True 重新打包即可。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # exe / 任务栏 / 开始菜单 / 桌面快捷方式 用的图标都来自这里。
    # 源图是仓库根的 icon.png，转成多尺寸 ico 存在 packaging/ 下（见该目录的说明）。
    icon=str(ROOT / "packaging" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CallClassroom",
)
