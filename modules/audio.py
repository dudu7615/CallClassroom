"""教室声卡直连的音频引擎。

服务端进程直接占用这台机器的默认输入/输出设备：

- **采集**：``InputStream`` 的回调把教室麦克风的 Int16 PCM 帧推给所有订阅者
  （操作者浏览器）。没有订阅者时数据直接丢弃，不做任何缓冲。
- **播放**：操作者传来的 PCM 进入 ``_play_q``，``OutputStream`` 的回调按需取用；
  缓冲不足时用静音补齐，绝不阻塞声卡回调线程。

回放缓冲积压过多时直接丢帧而不是无限增长——实时通话里"迟到"比"丢帧"更难听。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

import numpy as np
from loguru import logger
from numpy.typing import NDArray

RATE = 16000
CHANNELS = 1
DTYPE = "int16"

#: 候选采样率，从高到低取第一个输入输出设备都支持的。
#: 不能写死：教室电脑的真实 ALSA 设备通常只支持 44.1/48kHz，16kHz 只有经过
#: PipeWire/PulseAudio 的 default/pulse 设备才行。
CANDIDATE_RATES = (48000, 44100, 32000, 22050, 16000, 8000)

#: 每块音频的时长。块大小按采样率换算，保证延迟不随采样率漂移。
BLOCK_MS = 100
BLOCKSIZE = RATE * BLOCK_MS // 1000  # 16kHz 下的默认值，等价于 1600

#: 回放缓冲上限（块数）。每块 100ms，200 块 ≈ 20 秒的极限积压。
PLAY_QUEUE_MAX = 200

#: 每个订阅者的接收队列上限（帧数），满了丢帧以压低延迟。
SUBSCRIBER_QUEUE_MAX = 50

#: 声卡回调拿到的样本数组类型
Samples = NDArray[np.int16]


class AudioEngine:
    """封装 sounddevice 的采集/播放流，线程安全。

    构造时不碰任何硬件，必须显式调用 :meth:`start`。声卡不可用时 ``start``
    返回 ``False`` 并把原因写进 :attr:`error`，服务端据此降级为纯 WebSocket
    模式（前端仍可连上，只是没有教室声音）。
    """

    def __init__(
        self,
        rate: int | None = None,
        channels: int = CHANNELS,
        blocksize: int | None = None,
        in_device: int | str | None = None,
        out_device: int | str | None = None,
    ) -> None:
        # rate 传 None 表示开设备时按设备能力自动挑（见 pick_rate）
        self.rate = rate or RATE
        self._auto_rate = rate is None
        self.channels = channels
        self.blocksize = blocksize or self.blocksize_for(self.rate)
        self.in_device = in_device
        self.out_device = out_device

        self.available = False
        self.error: str | None = None
        self.in_name: str | None = None
        self.out_name: str | None = None

        self._in_stream: Any = None
        self._out_stream: Any = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._subs_lock = threading.Lock()

        self._play_q: queue.Queue[Samples] = queue.Queue(maxsize=PLAY_QUEUE_MAX)
        self._tail: Samples | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """打开输入/输出流。返回是否成功。"""
        if self.available:
            return True
        try:
            # 延迟导入：没装 PortAudio 时 import 就会抛错，不能让整个模块挂掉
            import sounddevice as sd
        except Exception as exc:
            self.error = f"sounddevice 不可用：{exc}"
            logger.error("{}（将以无声卡模式运行）", self.error)
            return False

        def label(device: int | str | None, fallback: str) -> str:
            try:
                return str(sd.query_devices(device)["name"])
            except Exception:
                return fallback

        try:
            in_index = self.resolve_device(self.in_device, want_input=True)
            out_index = self.resolve_device(self.out_device, want_input=False)
            if self._auto_rate:
                self.rate = self.pick_rate(in_index, out_index)
                self.blocksize = self.blocksize_for(self.rate)

            self._in_stream = sd.InputStream(
                samplerate=self.rate,
                channels=self.channels,
                dtype=DTYPE,
                blocksize=self.blocksize,
                device=in_index,
                callback=self._in_callback,
            )
            self._out_stream = sd.OutputStream(
                samplerate=self.rate,
                channels=self.channels,
                dtype=DTYPE,
                blocksize=self.blocksize,
                device=out_index,
                callback=self._out_callback,
            )
            self._in_stream.start()
            self._out_stream.start()
        except Exception as exc:
            self.error = f"打开音频设备失败：{exc}"
            logger.error("{}（将以无声卡模式运行）", self.error)
            self.stop()
            return False

        self.in_name = label(self._in_stream.device, "默认输入设备")
        self.out_name = label(self._out_stream.device, "默认输出设备")
        self.available = True
        logger.info(
            "音频引擎就绪 {}Hz/{}ch：采集={} 播放={}",
            self.rate,
            self.channels,
            self.in_name,
            self.out_name,
        )
        return True

    @staticmethod
    def resolve_device(device: int | str | None, *, want_input: bool) -> int | None:
        """把配置里的设备名解析成 sounddevice 索引。

        配置存名字而不是索引，因为索引会随插拔和重启漂移。找不到、或者同名设备
        没有对应方向的通道时返回 ``None``，让 sounddevice 用系统默认设备——
        宁可退默认也不要开不了设备。
        """
        if device is None:
            return None
        if isinstance(device, int):
            return device
        try:
            import sounddevice as sd
        except Exception:
            return None

        channel_key = "max_input_channels" if want_input else "max_output_channels"
        for index, info in enumerate(sd.query_devices()):
            if info["name"] == device and info[channel_key] > 0:
                return index
        logger.warning("配置的设备 {!r} 不存在或没有该方向的通道，改用系统默认设备", device)
        return None

    @staticmethod
    def blocksize_for(rate: int) -> int:
        """按采样率换算块大小，让每块音频的时长恒为 ``BLOCK_MS``。"""
        return max(1, rate * BLOCK_MS // 1000)

    @staticmethod
    def pick_rate(in_index: int | None, out_index: int | None) -> int:
        """挑一个输入、输出设备都支持的采样率。

        真实的 ALSA 设备多数只支持 44.1/48kHz，所以这里逐个试而不是写死。
        都不支持时退回 :data:`RATE`，让 ``start()`` 去报真正的错误。
        """
        try:
            import sounddevice as sd
        except Exception:
            return RATE

        for rate in CANDIDATE_RATES:
            try:
                sd.check_input_settings(
                    device=in_index, channels=CHANNELS, dtype=DTYPE, samplerate=rate
                )
                sd.check_output_settings(
                    device=out_index, channels=CHANNELS, dtype=DTYPE, samplerate=rate
                )
            except Exception:
                continue
            return rate

        logger.warning("找不到输入输出都支持的采样率，退回 {}Hz", RATE)
        return RATE

    def reconfigure(self, in_device: str | None, out_device: str | None) -> bool:
        """换设备。声卡开着就立刻重开，没开着就只记下参数等下次打开。

        返回重开之后设备是否可用。
        """
        self.in_device = in_device
        self.out_device = out_device
        if not self.available:
            return False
        logger.info(
            "切换教室声卡：麦克风={} 音响={}",
            in_device or "系统默认",
            out_device or "系统默认",
        )
        self.flush()  # 旧设备的残留音频不该在新设备上放出来
        self.stop()
        return self.start()

    def stop(self) -> None:
        was_open = self.available
        for stream in (self._in_stream, self._out_stream):
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.opt(exception=True).debug("关闭音频流时出错")
        self._in_stream = None
        self._out_stream = None
        self.available = False
        # 一并清掉设备名：留着会让人以为设备还开着
        self.in_name = None
        self.out_name = None
        if was_open:
            logger.info("已释放教室声卡")

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        """列出本机音频设备，供 /api/devices 展示与排障。"""
        try:
            import sounddevice as sd
        except Exception:
            return []
        devices: list[dict[str, Any]] = []
        for index, info in enumerate(sd.query_devices()):
            devices.append(
                {
                    "index": index,
                    "name": str(info["name"]),
                    "inputs": int(info["max_input_channels"]),
                    "outputs": int(info["max_output_channels"]),
                    "default_rate": int(info["default_samplerate"]),
                }
            )
        return devices

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[bytes]:
        """注册一个教室麦克风数据的消费者，返回接收队列。"""
        self._loop = loop
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        with self._subs_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        with self._subs_lock:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._subs_lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------ 声卡回调

    def _in_callback(
        self,
        indata: Samples,
        _frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        """输入回调：把麦克风帧派发给所有订阅者。"""
        if status:
            logger.debug("输入流状态：{}", status)
        loop = self._loop
        if loop is None:
            return
        with self._subs_lock:
            if not self._subscribers:
                return
            subs = list(self._subscribers)
        frame = indata.tobytes()
        for q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, frame)
            except RuntimeError:
                # 事件循环已关闭，丢弃即可
                return

    @staticmethod
    def _offer(q: asyncio.Queue[bytes], frame: bytes) -> None:
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            # 消费者跟不上，丢掉这一帧换取低延迟
            pass

    def _out_callback(
        self,
        outdata: Samples,
        frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        """输出回调：从回放缓冲取数据填满 outdata，不足补零。"""
        if status:
            logger.debug("输出流状态：{}", status)
        need = frames * self.channels
        flat = outdata.reshape(-1)
        flat[:] = 0
        filled = 0

        tail = self._tail
        if tail is not None and tail.size:
            take = min(need, tail.size)
            flat[:take] = tail[:take]
            self._tail = tail[take:] if tail.size > take else None
            filled = take

        while filled < need:
            try:
                block = self._play_q.get_nowait()
            except queue.Empty:
                break
            take = min(need - filled, block.size)
            flat[filled : filled + take] = block[:take]
            filled += take
            if block.size > take:
                self._tail = block[take:]
                break

    # ------------------------------------------------------------------ 回放

    def play(self, data: bytes) -> None:
        """把操作者传来的一段 Int16 PCM 排入回放缓冲。"""
        if not data:
            return
        arr: Samples = np.frombuffer(data, dtype=np.int16)
        if not arr.size:
            return
        try:
            self._play_q.put_nowait(arr)
        except queue.Full:
            logger.warning("回放缓冲已满，丢弃一帧（上行速度超过声卡消费速度）")

    def flush(self) -> None:
        """清空回放缓冲，用于打断正在播放的喊话。"""
        self._tail = None
        while True:
            try:
                self._play_q.get_nowait()
            except queue.Empty:
                return

    @property
    def pending(self) -> int:
        return self._play_q.qsize()

    # ------------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        return {
            "engine_available": self.available,
            "error": self.error,
            "rate": self.rate,
            "channels": self.channels,
            "blocksize": self.blocksize,
            # device_* 是当前实际打开的设备（没开就是 None）
            "device_in": self.in_name,
            "device_out": self.out_name,
            # selected_* 是配置里选定的设备名，None 表示跟随系统默认
            "selected_in": self.in_device,
            "selected_out": self.out_device,
            "subscribers": self.subscriber_count,
        }
