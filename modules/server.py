"""CallClassroom 服务端：FastAPI 应用 + WebSocket 音频通路。

拓扑::

    操作者浏览器 ──WS(二进制 PCM)──► 本进程 ──sounddevice──► 教室音响   （喊话）
    操作者耳机   ◄──WS(二进制 PCM)── 本进程 ◄──sounddevice── 教室麦克风 （收音）

半双工：任一操作者按住「说话」时，教室麦克风的上行会被屏蔽，避免教室音响
放出的喊话被麦克风拾回、再传回操作者耳朵形成回声／啸叫。

声卡按需开关：第一个操作者连进来时打开，最后一个断开时释放，没人在线时教室
电脑的麦克风和音响是空闲的，其他软件可以正常独占使用。

WebSocket 协议（``/ws``）::

    客户端 → 服务端
        {"type": "ptt", "on": true|false}   按下/松开说话键
        {"type": "flush"}                   清空服务端回放缓冲
        二进制帧                             操作者麦克风的 Int16 PCM（仅按住时发送）

    服务端 → 客户端
        {"type": "state", ...}              连接建立时的引擎状态
        {"type": "peer", "talking": bool}   喊话状态变化
        二进制帧                             教室麦克风的 Int16 PCM
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .audio import AudioEngine
from .settings import DeviceSelection, SettingsStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

OUT_QUEUE_MAX = 50

audio = AudioEngine()
store = SettingsStore()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 载入选定的设备。声卡本身不在启动时打开：等第一个操作者连进来再开，
    # 最后一个走了就关，免得没人在线时教室电脑的麦克风指示灯一直亮着。
    selection = store.load()
    audio.in_device = selection.in_device
    audio.out_device = selection.out_device
    yield
    audio.stop()  # 兜底：关停时若还有连接，把设备释放掉


app = FastAPI(title="CallClassroom", version="0.1.0", lifespan=lifespan)


class AudioHub:
    """管理所有操作者连接，并把教室麦克风的声音广播给他们。

    每个连接对应一个出站队列，队列里交替存放音频二进制帧（``bytes``）和控制
    消息（``str``），由各自的发送协程按类型分发。
    """

    def __init__(self, engine: AudioEngine) -> None:
        self.engine = engine
        self._queues: set[asyncio.Queue[bytes | str]] = set()
        self._talking: set[int] = set()
        self._mic_q: asyncio.Queue[bytes] | None = None
        self._pump: asyncio.Task[None] | None = None
        # 设备开关涉及 to_thread + 检查再动作，并发连接时必须串行化
        self._lifecycle = asyncio.Lock()

    # ---------------------------------------------------------------- 连接

    async def connect(self) -> asyncio.Queue[bytes | str]:
        q: asyncio.Queue[bytes | str] = asyncio.Queue(maxsize=OUT_QUEUE_MAX)
        self._queues.add(q)
        await self._open_device()
        return q

    async def disconnect(self, q: asyncio.Queue[bytes | str]) -> None:
        self._queues.discard(q)
        self._talking.discard(id(q))
        if not self._queues:
            await self._close_device()

    def broadcast(self, payload: dict[str, Any]) -> None:
        """把一条控制消息投递给所有连接。"""
        message = json.dumps(payload)
        for q in list(self._queues):
            with suppress(asyncio.QueueFull):
                q.put_nowait(message)

    # ---------------------------------------------------------------- 喊话状态

    def set_talking(self, q: asyncio.Queue[bytes | str], on: bool) -> bool:
        """标记某个连接是否正在喊话，返回整体状态是否发生了变化。"""
        before = self.talking
        if on:
            self._talking.add(id(q))
        else:
            self._talking.discard(id(q))
        return before != self.talking

    @property
    def talking(self) -> bool:
        return bool(self._talking)

    # ---------------------------------------------------------------- 设备生命周期

    async def _open_device(self) -> None:
        """第一个操作者连进来时打开教室声卡并启动麦克风泵。"""
        async with self._lifecycle:
            if self._pump is not None:
                return
            # 先订阅再开设备：订阅会记下事件循环，早于设备开始回调
            loop = asyncio.get_running_loop()
            self._mic_q = self.engine.subscribe(loop)
            # 开设备可能要几十毫秒，扔到线程里别卡住事件循环
            await asyncio.to_thread(self.engine.start)
            self._pump = asyncio.create_task(self._pump_mic())

    async def _close_device(self) -> None:
        """最后一个操作者走了，把声卡交还系统。"""
        async with self._lifecycle:
            if self._pump is not None:
                self._pump.cancel()
                with suppress(asyncio.CancelledError):
                    await self._pump
                self._pump = None
            if self._mic_q is not None:
                self.engine.unsubscribe(self._mic_q)
                self._mic_q = None
            await asyncio.to_thread(self.engine.stop)

    async def apply_devices(self, selection: DeviceSelection) -> None:
        """设备选择变了：声卡开着就立刻换过去，没开就只记下参数。"""
        async with self._lifecycle:
            await asyncio.to_thread(
                self.engine.reconfigure, selection.in_device, selection.out_device
            )

    async def _pump_mic(self) -> None:
        assert self._mic_q is not None
        mic_q = self._mic_q
        while True:
            frame = await mic_q.get()
            if self.talking:
                # 半双工：喊话期间不回传教室声音，防止回声/啸叫
                continue
            for q in list(self._queues):
                with suppress(asyncio.QueueFull):
                    q.put_nowait(frame)

    # ---------------------------------------------------------------- 状态

    def status(self) -> dict[str, Any]:
        return {"clients": len(self._queues), "talking": self.talking}


hub = AudioHub(audio)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    """引擎与连接状态，供前端展示与排障。"""
    return {**audio.status(), **hub.status()}


@app.get("/api/devices")
async def api_devices() -> dict[str, Any]:
    """本机可见的音频设备列表，外加当前选定的麦克风/音响。"""
    return {
        "devices": audio.list_devices(),
        "selected": DeviceSelection(
            in_device=audio.in_device if isinstance(audio.in_device, str) else None,
            out_device=audio.out_device if isinstance(audio.out_device, str) else None,
        ).model_dump(),
    }


@app.post("/api/devices/select")
async def api_select_devices(payload: DeviceSelection) -> dict[str, Any]:
    """切换教室侧的麦克风和音响，并持久化到 settings.json。

    声卡开着的话会立刻重开；打不开时 ``status.error`` 会说明原因，
    页面照常可用，只是没有教室声音。
    """
    store.save(payload)
    await hub.apply_devices(payload)
    status = await api_status()
    hub.broadcast({"type": "state", **status})
    logger.info(
        "操作者切换教室设备：麦克风={} 音响={}",
        payload.in_device or "系统默认",
        payload.out_device or "系统默认",
    )
    return {"selected": payload.model_dump(), "status": status}


@app.websocket("/ws")
async def ws_audio(ws: WebSocket) -> None:
    await ws.accept()
    out = await hub.connect()

    async def pump() -> None:
        """把教室麦克风的音频和广播消息推给这个浏览器。"""
        while True:
            item = await out.get()
            if isinstance(item, str):
                await ws.send_text(item)
            else:
                await ws.send_bytes(item)

    pump_task = asyncio.create_task(pump())
    try:
        await ws.send_json({"type": "state", **await api_status()})
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is not None:
                # 声卡没开就别往回放缓冲里塞：没人消费只会撑满后刷警告
                if hub.talking and audio.available:
                    audio.play(data)
                continue

            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("忽略无法解析的控制消息：{}", text[:120])
                continue

            match payload.get("type"):
                case "ptt":
                    on = bool(payload.get("on"))
                    # 按下和松开都清一次缓冲：开始时不带上残留，结束时立刻静音
                    audio.flush()
                    if hub.set_talking(out, on):
                        hub.broadcast({"type": "peer", "talking": hub.talking})
                case "flush":
                    audio.flush()
                case "ping":
                    await ws.send_json({"type": "pong"})
                case _:
                    logger.debug("忽略未知控制消息：{}", payload.get("type"))
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task
        await hub.disconnect(out)
        hub.broadcast({"type": "peer", "talking": hub.talking})


# 静态页面挂载必须放在所有路由之后：Starlette 按注册顺序匹配。
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
