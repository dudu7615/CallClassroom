# packaging/

Windows 打包。**只能在 Windows 上执行**（PyInstaller 不支持交叉编译）：

```powershell
uv sync --group build
uv run pyinstaller packaging/CallClassroom.spec --noconfirm --clean   # → dist/CallClassroom/
ISCC.exe /DAppVersion=0.1.0 packaging\setup.iss                       # → dist\installer\*.exe
```

`.github/workflows/release.yml` 走的就是这两步，合并进 `main` 后自动执行。

## 图标

源图是仓库根的 `icon.png`（1024×1024）。**改图标时改那个文件，然后重新生成
下面两个派生文件**，它们和源图一起提交，这样 CI 不需要 Pillow：

| 文件 | 用途 | 怎么来的 |
|---|---|---|
| `icon.ico` | exe、快捷方式、安装器（`SetupIconFile` 只吃真正的 ico，png 不行） | 见下 |
| `../web/icon.png` | 浏览器标签页 / 手机添加到主屏幕 | 见下 |

```bash
uv run --with pillow python - <<'PY'
from PIL import Image

src = Image.open("icon.png").convert("RGBA")
src.save(
    "packaging/icon.ico",
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
src.resize((256, 256), Image.Resampling.LANCZOS).save("web/icon.png", optimize=True)
PY
```

ICO 里塞多个尺寸是必要的：Windows 会按显示场景（任务栏 16px、资源管理器大图标
256px）自己挑一档，只放单一尺寸会糊。

## 几个不能改的东西

- `setup.iss` 里的 **`AppId`**：软件的唯一标识，改了等于换了另一个程序，老版本
  的覆盖安装和卸载都会错乱。
- `CallClassroom.spec` 里 `web/` 的 **dest 必须写 `"web"`**：`server.py` 的
  `WEB_DIR` 打包后解析到 `_internal/web`，对不上就是 404。原因见
  `modules/paths.py` 的模块文档串。
