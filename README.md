# CallClassroom

对教室电脑喊话和收音的单页工具。服务端直连教室电脑的声卡（不是浏览器），
操作者用手机或笔记本打开页面即可：

- **喊话**：按住「说话」→ 操作者麦克风采样 → 服务端 → 教室音响
- **收音**：教室麦克风 → 服务端 → 操作者耳机

## 架构

```
操作者浏览器 ──WebSocket(Int16 PCM)──► 本进程 ──sounddevice──► 教室音响    喊话
操作者耳机   ◄──WebSocket(Int16 PCM)── 本进程 ◄──sounddevice── 教室麦克风  收音
```

全链路单声道 / Int16，服务端不做编解码，只做转发与缓冲，所以延迟主要来自
100ms 的分片和浏览器侧的抖动缓冲。

**采样率不写死**，开设备时按声卡能力协商：按 48k → 44.1k → 32k → 22.05k →
16k → 8k 的顺序，取第一个输入输出都支持的。教室电脑的真实 ALSA 设备多数只支持
44.1/48kHz，写死 16kHz 会直接开不了设备（`Invalid sample rate`）。
协商结果通过 WebSocket 的 `state` 消息下发给页面，浏览器按它建 `AudioContext`。

### 半双工

教室音响放出的喊话会被教室麦克风拾回，再传回操作者耳朵，形成回声甚至啸叫。
两端各掐一道：

1. 服务端在任一操作者按住说话时，**停止下发**教室麦克风的声音；
2. 页面在按住时**本地静音**，掐掉服务端停发之前已经排进播放队列的那几帧。

### 选择教室设备

页面上的「教室麦克风」和「教室音响」两个下拉框选的是**教室电脑的**声卡设备
（不是操作者自己的）。教室电脑接了 USB 会议全麦、功放或者多个 HDMI 输出时，
在这里选。选项默认跟随系统默认设备。

选择存在服务端的 `settings.json` 里，重启服务也在。只存**设备名**不存索引——
索引会随插拔和重启漂移。开设备时把名字解析成索引，找不到就退回系统默认并在
页面上标出「所选设备未找到」。

切换是**立即生效**的：有人在线时服务端会重开声卡。因为采样率是随设备协商的，
如果新设备的采样率变了，页面会自动重建音频通路（浏览器里 `AudioContext` 的
采样率建好就不能改）。

> ⚠️ 任何能打开这个页面的人都能改教室设备。目前没有鉴权——局域网内谁都能访问，
> 也就谁都能改。需要限制的话得另加访问口令。

### 声卡占用

**声卡是按需开关的**：第一个操作者连进来时打开，最后一个断开时释放。没人在线
时教室电脑的麦克风和音响是空闲的，麦克风指示灯不亮，其他软件可以正常使用。

如果教室电脑跑的是 PipeWire / PulseAudio（桌面 Linux 的常态），设备本来就是
多程序共享的，我们的程序开着也不影响别的软件。真正独占只发生在绕开声音服务器
直接用 `hw:` 设备时——那种情况下 `start()` 会拿到 `PortAudioError`，服务端降级
成「无声卡模式」并把原因写进日志和页面，连接本身不受影响。

## 环境要求

- Python ≥ 3.14，[uv](https://docs.astral.sh/uv/)
- **PortAudio**（`sounddevice` 的系统依赖）：
  ```bash
  sudo apt install libportaudio2 portaudio19-dev     # Debian / Ubuntu / Kali
  sudo pacman -S portaudio                            # Arch
  brew install portaudio                              # macOS
  ```
  没装时 `import sounddevice` 就会失败，服务端会降级到「无声卡模式」——
  页面照常打开，只是教室没声音，`/api/status` 和页面顶部会给出具体原因。

## 运行

```bash
uv sync
uv run main.py                 # https://0.0.0.0:8000（默认自签证书，见下一节）
uv run main.py --no-tls        # 明文 http://0.0.0.0:8000，仅本机调试用
uv run main.py --port 9000 -v  # 换端口 + 调试日志
uv run main.py --reload        # 开发模式，改代码自动重启
```

启动后会打印本机所有可访问的 URL。

自签证书第一次运行时生成，之后一直复用（有效期 10 年）。**地址变了才会重签**——
证书的 SAN 里写死了生成时的网卡 IP，换了网络导致 IP 变化时会重新生成一张，
浏览器里之前点过的「继续前往」可能得再点一次。

证书是用 [`cryptography`](https://cryptography.io/) 现签的，**不需要系统装
openssl** 或别的外部命令；除了 PortAudio（见上一节，且没装也只是没声音），
`uv sync` 之后就能直接跑。

## Windows 安装包

给教室那台没有 Python 的机器用。**合并进 `main` 后 GitHub Actions 会自动打包**，
产物在 Actions 的 Artifacts 里；`pyproject.toml` 的 `version` 是新的（还没有同名
tag）时会顺带建一个 Release。所以发版本的流程就是：在 PR 里改 `version` → 合并。

产物有两个：

- `CallClassroom-Setup-<版本>.exe` —— 安装包
- `CallClassroom-portable-<版本>.zip` —— 免安装版，解压双击 `CallClassroom.exe`

本机手动打（**只能在 Windows 上打，PyInstaller 不支持交叉编译**）：

```powershell
uv sync --group build
uv run pyinstaller packaging/CallClassroom.spec --noconfirm --clean
```

安装包的行为：

- 装到 `%LOCALAPPDATA%\Programs\CallClassroom`，**只对当前用户、不弹 UAC**。
- 默认创建桌面快捷方式，并且**默认开机自启**（装的时候可以取消勾选）。
- 卸载不会动用户数据。设备和证书都放在 `%LOCALAPPDATA%\CallClassroom`，
  与安装目录分开，所以覆盖安装和卸载都不会丢。

⚠️ **首次运行 Windows 会弹一次"是否允许 CallClassroom 访问网络"，要点"允许"。**
不点的话本机访问正常、但局域网里其他设备连不上——因为安装过程不提权，装不了
防火墙规则。另外程序是**没有窗口的后台服务**，出问题看不到任何提示，日志在
`%LOCALAPPDATA%\CallClassroom\callclassroom.log`。

## ⚠️ 跨设备访问必须用 HTTPS

浏览器只在**安全上下文**下开放 `getUserMedia`（麦克风）和 `AudioWorklet`。
安全上下文 = `localhost`，或者 `https://`。

所以从手机访问 `http://192.168.1.20:8000` 时，麦克风会被直接禁用，页面顶部
会明确提示。三种解法，任选其一：

| 方案 | 做法 | 适用 |
|---|---|---|
| **自签证书**（默认，推荐） | 直接 `uv run main.py`，浏览器首次访问点「继续前往」 | 一次性配置，手机也能用 |
| Chrome 白名单 | 在操作者电脑的 `chrome://flags/#unsafely-treat-insecure-origin-as-secure` 里填 `http://192.168.1.20:8000` 并启用 | 仅桌面 Chrome，改完要重启浏览器 |
| 正式证书反代 | Caddy / nginx 终止 TLS，转发到本服务 | 有域名时最省事 |

只有**在教室电脑本机**用 `http://localhost:8000` 打开时才不需要 https——这种情况
加 `--no-tls` 可以省掉那个证书警告。

## 操作流程

1. 教室电脑上启动服务（此时还没占用声卡）；
2. 操作者在自己的设备上打开页面，点 **启动音频**（必须点一次，浏览器才解锁
   音频并弹麦克风授权）。**页面一连上，服务端就打开教室声卡**；
3. 页面上选好教室的麦克风和音响——设备名会显示在下方，采样率也一并标出；
4. 「教室声音」电平条随教室动静跳动，勾选 **监听** 就能听到教室；
5. **按住** 大按钮说话，教室音响实时出声；松手即停。

按住期间显示「他人正在喊话」表示有别的操作者也在说话（此时教室声音会被掐断）。
所有操作者都关掉页面后，服务端会释放声卡。多个人同时在线时设备只打开一次。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| 页面提示「服务端音频引擎未启动」 | 没装 PortAudio，或教室电脑没有可用声卡。看 `uv run main.py -v` 的日志 |
| 日志里 `Invalid sample rate [PaErrorCode -9997]` | 选中的设备不支持协商出的采样率。正常情况下 `pick_rate()` 会规避；若仍出现，说明该设备的 `check_*_settings` 报得不准，换一个设备 |
| 选了设备但没生效 | 页面下方会注明「所选设备未找到，已回退默认」——说明存的设备名在列表里不存在（被拔了？）。选一次别的再选回来即可 |
| 点启动音频没弹麦克风授权 | 非安全上下文，见上一节 |
| 教室没声音 | 看页面下方显示的实际设备名对不对；`uv run python -c "import sounddevice; print(sounddevice.query_devices())"` 可列全部设备 |
| 有回声 / 啸叫 | 说明半双工没生效，检查服务端日志里是否有 `talking` 状态；操作者戴耳机可直接消除 |
| 断音、断续 | 多半是 WiFi 抖动。可调小 `modules/audio.py` 的 `BLOCK_MS` 或调大 `web/app.js` 的 `MAX_LEAD` |

## 接口

| 路径 | 说明 |
|---|---|
| `GET /` | 操作者页面 |
| `GET /api/status` | 引擎与连接状态（含采样率、选定的与实际打开的设备） |
| `GET /api/devices` | 本机音频设备列表 + 当前选定的麦克风/音响 |
| `POST /api/devices/select` | 切换教室侧设备并持久化，`{"in_device": 名字或null, "out_device": ...}` |
| `WS /ws` | 音频与控制通道，协议见 `modules/server.py` 顶部注释 |

## 代码规范

两道门禁，改动后都要过：

```bash
uv run pytest -q                       # 单元测试
uv run basedpyright                    # 类型检查，strict 模式，0 error
```

- **类型**：`pyrightconfig.json` 里 `typeCheckingMode: strict`。`sounddevice`
  上游没有 `py.typed`，类型桩放在 `typings/sounddevice.pyi`（`stubPath` 已指向那里）。
- **日志**：全程用 [loguru](https://github.com/Delgan/loguru)，不用 `print`，
  也不直接用标准库 `logging` 打日志。uvicorn 自己的日志由
  `modules/logsetup.py` 的 handler 转发进 loguru，所以终端里只有一种格式。
  ⚠️ 因此 `uvicorn.run()` 必须传 `log_config=None`，否则 uvicorn 启动时
  会用 `dictConfig` 重置 `uvicorn.*` 的 logger，把转发 handler 冲掉。

## 测试

```bash
uv run pytest -q
```

覆盖回放缓冲的拼帧/补齐/丢帧、麦克风帧的订阅派发、半双工门控与掉线清理。
不需要真实声卡——测试直接把假数据喂给声卡回调方法。

## 目录

```
main.py                启动入口（参数解析、自签证书、uvicorn）
icon.png               应用图标源图（1024×1024，派生文件见 packaging/README.md）
pyrightconfig.json     类型检查配置（strict）
settings.json          本机选定的教室设备（自动生成，已 gitignore）
modules/paths.py       运行期目录解析（程序资源 vs 用户数据，打包后分开）
modules/audio.py       sounddevice 采集/播放引擎
modules/server.py      FastAPI 应用、WebSocket 协议、连接管理
modules/settings.py    设备选择的持久化
modules/logsetup.py    loguru 配置 + uvicorn 日志转发
typings/sounddevice.pyi  sounddevice 类型桩
web/index.html         操作者页面
web/app.js             采集、播放排程、按住说话、设备选择
web/pcm-worklet.js     采集侧 AudioWorklet（重采样 + 门控）
web/icon.png           网页图标（由 icon.png 派生）
packaging/             Windows 打包（PyInstaller spec + Inno Setup 脚本 + 图标）
.github/workflows/     CI（合并进 main 出安装包）
tests/test_audio.py    音频引擎与连接管理测试
tests/test_settings.py 设备选择持久化测试
```
