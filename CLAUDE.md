# CLAUDE.md

给 Claude Code 在本仓库工作时的指引。面向使用者的完整文档在 [README.md](README.md)。

## 项目

CallClassroom：单页的教室对讲工具。服务端进程**直接占用教室电脑的声卡**
（不用浏览器采集），操作者从局域网打开页面喊话和收音。

## 常用命令

```bash
uv run main.py                 # 启动（默认自签证书 + https://0.0.0.0:8000）
uv run main.py --reload        # 热重载
uv run main.py --no-tls        # 退回明文 http，仅本机调试
uv run pytest -q               # 单元测试（不需要真实声卡）
uv run basedpyright            # 类型检查
uv run ruff check .            # 静态检查（全量规则，见 pyproject.toml 的放行清单）
uv run ruff format --check .   # 格式检查（改的时候去掉 --check）
```

打 Windows 安装包（**只能在 Windows 上打**，PyInstaller 不支持交叉编译）：

```powershell
uv sync --group build
uv run pyinstaller packaging/CallClassroom.spec --noconfirm --clean   # → dist/CallClassroom/
ISCC.exe /DAppVersion=0.1.0 packaging\setup.iss                       # → dist/installer/*.exe
```

改图标只改根目录的 `icon.png`，派生文件（`packaging/icon.ico`、`web/icon.png`）
要重新生成并一起提交，命令见 `packaging/README.md`。

改完任何 Python 代码，**必须**跑 `uv run basedpyright`、`uv run ruff check .` 和 `uv run pytest -q`。

## 硬性规范

- **类型必须过 `strict`**。`pyrightconfig.json` 已配好，目标是 0 error。
  第三方库没有类型桩时，在 `typings/` 下写 `.pyi`（见
  `typings/sounddevice.pyi`），**不要**去关检查规则或加 `# type: ignore`。
- **日志只用 loguru**：`from loguru import logger`。**禁止 `print`**。
  标准库 `logging` 只允许出现在 `modules/logsetup.py` 的转发层。
- 注释和文档字符串用中文，与现有代码保持一致。

## 数据流

```
操作者浏览器 ──WS(Int16 PCM)──► 服务端 ──sounddevice──► 教室音响    喊话
操作者耳机   ◄──WS(Int16 PCM)── 服务端 ◄──sounddevice── 教室麦克风  收音
```

全链路单声道 / Int16，服务端不编解码，只转发。**采样率是协商出来的，不是常量**：
`AudioEngine.pick_rate()` 按 48k→44.1k→32k→22.05k→16k→8k 取第一个输入输出都
支持的，块大小按 `BLOCK_MS` 换算。结果通过 `state` 消息下发给页面，浏览器用
它建 `AudioContext`。

WebSocket 协议（`/ws`）的完整定义在 `modules/server.py` 的模块文档字符串里。

## 容易踩的坑

这些都是已经踩过一次的，改动相关代码前先读：

1. **`uvicorn.run()` 必须传 `log_config=None`**。否则 uvicorn 启动时用
   `dictConfig` 重置 `uvicorn.*` 的 logger，把 `modules/logsetup.py` 挂的
   loguru 转发 handler 冲掉，uvicorn 的日志就消失了。

2. **浏览器只在安全上下文开放麦克风和 AudioWorklet**（`localhost` 或
   `https://`）。从别的设备用 `http://192.168.x.x:8000` 访问会被直接拒绝。
   所以服务端**默认就开 https**（`main.py` 自动签自签证书），页面里
   `requireSecureContext()` 也会提示。
   改动 `web/app.js` 时不要假设可以用 `http://` + IP 调试。

3. **半双工是双向的，改一处要同时改另一处**。教室音响放出的喊话会被教室
   麦克风拾回形成啸叫，所以：服务端在有人喊话时停发教室声音
   （`AudioHub._pump_mic` 里的 `if self.talking: continue`），页面在按住时
   还要本地静音（`web/app.js` 的 `setHolding`），因为服务端停发之前已经排进
   播放队列的帧仍会播出来。

4. **`sounddevice` 是延迟导入的**。没装 PortAudio 时 `import sounddevice`
   本身就抛错，所以只能在 `AudioEngine.start()` 内部 import，让服务端能降级到
   "无声卡模式"。别把它提到模块顶层。

5. **声卡回调里不能阻塞**。`_in_callback` / `_out_callback` 跑在 PortAudio 的
   实时线程上，回放缓冲满了要丢帧、不足要补静音，绝不能等待或抛异常。

6. **`modules/SqlDantic/` 被 pyright 排除了**。它是独立的嵌套 git 仓库，
   当前无人 import，strict 下有 115 个错误（元编程 + sqlalchemy 类型连锁）。
   要真正接入它时再一并修，别默认它已经可用。

7. **声卡按需开关，别在 lifespan 里打开设备**。`AudioHub._open_device()` 在第一个
   操作者连进来时开设备，`_close_device()` 在最后一个断开时释放——
   没人在线时教室麦克风不该被占着。因此 `connect()` / `disconnect()` 都是
   **async** 的，且内部用 `asyncio.Lock` 串行化（并发连接会同时走到"检查再动作"）。
   开设备要 `await asyncio.to_thread(engine.start)`，直接调会卡住事件循环。
   另外 `stop()` 会清空 `in_name`/`out_name`，免得状态里显示着设备名却其实是关的。

8. **采样率不能写死**。教室电脑的真实 ALSA 设备多数只支持 44.1/48kHz，写死
   16000 会直接 `Invalid sample rate [PaErrorCode -9997]` 开不了设备——之前能用
   纯粹是因为 `default` 设备走 PipeWire 帮我们重采样了。所以开设备前必须
   `pick_rate()` 协商，块大小随之用 `blocksize_for()` 换算。改 `web/app.js` 时
   注意 `state.rate` 来自服务端下发；采样率变了要重建 `AudioContext`
   （它的采样率建好就不能改），`teardownAudio()` 就是干这个的。

9. **设备选择存名字不存索引**。索引会随插拔和重启漂移。`resolve_device()` 在开
   设备时把名字解析成索引，解析不到就退回系统默认并在页面标出。改这块时别顺手
   把索引写进 `settings.json`。

10. **打包后"程序资源"和"用户数据"不在一个地方**。`modules/paths.py` 把这件事
    定死了：`web/` 这类只读资源用 `__file__` 相对定位（PyInstaller 会把打包进去
    的模块的 `__file__` 设成解包目录下的绝对路径，所以 `server.py` 的 `WEB_DIR`
    **不用改**，但 spec 里 data 的 dest 必须写成 `web` 才对齐）；`settings.json`
    / `.certs` / 日志这类要写的东西一律走 `DATA_DIR`，打包后落到
    `%LOCALAPPDATA%\CallClassroom`，**不要**再写 `Path(__file__).parent.parent`。
    另外打包后 `sys.stderr` 是 `None`（spec 里 `console=False`），往 stderr 挂
    loguru sink 之前必须先判空，否则启动即崩。

## 文件地图

| 文件 | 职责 |
|---|---|
| `main.py` | 参数解析、自签证书生成、uvicorn 启动 |
| `modules/paths.py` | 运行期目录解析：程序资源 vs 用户数据（打包后落到 `%LOCALAPPDATA%`） |
| `modules/audio.py` | `AudioEngine`：sounddevice 采集/播放 + 订阅分发 |
| `modules/server.py` | FastAPI 应用、`AudioHub` 连接管理、WebSocket 协议 |
| `modules/settings.py` | 教室设备选择的持久化（`settings.json`，已 gitignore） |
| `modules/logsetup.py` | loguru 配置 + uvicorn 日志转发 |
| `typings/sounddevice.pyi` | sounddevice 类型桩（上游无 `py.typed`） |
| `web/app.js` | 采集、播放排程、按住说话、教室设备选择 |
| `web/pcm-worklet.js` | 采集侧 AudioWorklet：重采样到服务端采样率 + 按压门控 |
| `packaging/CallClassroom.spec` | PyInstaller 打包配置（onedir） |
| `packaging/setup.iss` | Inno Setup 安装脚本（per-user 装到 `%LOCALAPPDATA%`） |
| `packaging/icon.ico` | exe/安装器图标，由根目录 `icon.png` 派生（见 `packaging/README.md`） |
| `.github/workflows/release.yml` | 合并进 main 出安装包 + 便携版 |
| `tests/test_audio.py` | 音频引擎与连接管理测试（假造声卡回调数据） |
| `tests/test_settings.py` | 设备选择持久化测试 |

## 测试约定

测试不碰真实硬件：直接调用 `AudioEngine._out_callback` / `_in_callback`，
把假的 numpy 数组当声卡数据喂进去。文件顶部的
`# pyright: reportPrivateUsage=false` 是刻意为之——访问私有成员是这里的测试手段。

文件里还有一个 autouse fixture `_block_sound_hardware`，把所有测试的
`import sounddevice` 变成失败。这样 `engine.start()` 一律走降级分支，
测试既不依赖运行环境有没有声卡，也不会真的占用开发机的麦克风和音响。
新增测试请沿用这个模式，不要去 mock `sounddevice` 模块。
