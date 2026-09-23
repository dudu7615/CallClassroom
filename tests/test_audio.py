"""音频引擎与连接的单元测试。

不碰真实硬件：直接调用 sounddevice 会回调的那两个方法，用假的 outdata/indata
验证拼帧、补齐、半双工门控这些最容易出错的地方。
"""

# 测试要直接戳声卡回调和连接内部状态，这是刻意的测试手段，不是误用。
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import sys
import types

import numpy as np
import pytest

from modules.audio import PLAY_QUEUE_MAX, RATE, AudioEngine
from modules.server import AudioHub

FRAMES = 160  # 每次回调的样本数


@pytest.fixture(autouse=True)
def _block_sound_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有测试都不碰真实声卡：让 `import sounddevice` 直接失败。

    这样 `AudioEngine.start()` 会立刻走降级分支，测试既不依赖运行环境有没有
    声卡，也不会真的占用开发机的麦克风和音响。
    """
    monkeypatch.setitem(sys.modules, "sounddevice", None)


def make_outdata(frames: int = FRAMES, channels: int = 1) -> np.ndarray:
    return np.full((frames, channels), 12345, dtype=np.int16)


def run_output(engine: AudioEngine, frames: int = FRAMES) -> np.ndarray:
    """跑一次输出回调，返回它写入的样本（展平后）。"""
    out = make_outdata(frames)
    engine._out_callback(out, frames, None, None)
    return out.reshape(-1)


def pcm(*values: int) -> bytes:
    return np.array(values, dtype=np.int16).tobytes()


# --------------------------------------------------------------------- 回放缓冲


def test_silence_when_buffer_empty() -> None:
    engine = AudioEngine()
    assert np.all(run_output(engine) == 0), "没数据时必须补静音，不能保留脏数据"


def test_single_block_fills_exactly() -> None:
    engine = AudioEngine()
    engine.play(pcm(*range(1, FRAMES + 1)))
    out = run_output(engine)
    assert list(out) == list(range(1, FRAMES + 1))
    assert engine.pending == 0


def test_leftover_carries_into_next_callback() -> None:
    """一个比回调块大的数据要分两次吐出来，顺序不能乱。"""
    engine = AudioEngine()
    total = FRAMES + 40
    engine.play(pcm(*range(1, total + 1)))

    first = run_output(engine)
    second = run_output(engine)

    assert list(first) == list(range(1, FRAMES + 1))
    assert list(second[:40]) == list(range(FRAMES + 1, total + 1))
    assert np.all(second[40:] == 0), "余量之后应是静音"


def test_multiple_blocks_drain_in_order() -> None:
    engine = AudioEngine()
    engine.play(pcm(*range(1, 101)))
    engine.play(pcm(*range(101, 201)))
    out = run_output(engine, frames=200)
    assert list(out) == list(range(1, 201))


def test_underrun_midway_fills_tail_with_silence() -> None:
    engine = AudioEngine()
    engine.play(pcm(*([7] * 50)))
    out = run_output(engine)
    assert np.all(out[:50] == 7)
    assert np.all(out[50:] == 0)


def test_flush_clears_pending_audio() -> None:
    engine = AudioEngine()
    engine.play(pcm(*range(1, 101)))
    engine.flush()
    assert engine.pending == 0
    assert np.all(run_output(engine) == 0)


def test_full_queue_drops_instead_of_blocking() -> None:
    """回放缓冲塞满时丢帧，绝不能阻塞声卡回调线程。"""
    engine = AudioEngine()
    for _ in range(PLAY_QUEUE_MAX + 50):
        engine.play(pcm(*([1] * 10)))
    assert engine.pending == PLAY_QUEUE_MAX


def test_empty_and_odd_payloads_are_ignored() -> None:
    engine = AudioEngine()
    engine.play(b"")
    assert engine.pending == 0
    engine.play(np.array([], dtype=np.int16).tobytes())
    assert engine.pending == 0


# --------------------------------------------------------------------- 采集派发


def test_frames_fan_out_to_all_subscribers() -> None:
    async def scenario() -> None:
        engine = AudioEngine()
        loop = asyncio.get_running_loop()
        q1 = engine.subscribe(loop)
        q2 = engine.subscribe(loop)

        indata = np.arange(FRAMES, dtype=np.int16).reshape(FRAMES, 1)
        engine._in_callback(indata, FRAMES, None, None)
        await asyncio.sleep(0)  # 让 call_soon_threadsafe 排的回调跑掉

        expected = indata.tobytes()
        assert await q1.get() == expected
        assert await q2.get() == expected

    asyncio.run(scenario())


def test_no_subscribers_means_no_buffering() -> None:
    engine = AudioEngine()
    engine._in_callback(np.zeros((FRAMES, 1), dtype=np.int16), FRAMES, None, None)
    assert engine.subscriber_count == 0


def test_unsubscribe_stops_delivery() -> None:
    async def scenario() -> None:
        engine = AudioEngine()
        loop = asyncio.get_running_loop()
        q = engine.subscribe(loop)
        engine.unsubscribe(q)

        engine._in_callback(np.zeros((FRAMES, 1), dtype=np.int16), FRAMES, None, None)
        await asyncio.sleep(0)
        assert q.empty()

    asyncio.run(scenario())


# --------------------------------------------------------------------- 半双工门控


def test_mic_forwarding_pauses_while_talking() -> None:
    """喊话期间教室麦克风不回传，否则会形成回声/啸叫。"""

    async def scenario() -> None:
        hub = AudioHub(AudioEngine())
        q = await hub.connect()
        assert hub._mic_q is not None
        mic = hub._mic_q

        mic.put_nowait(b"room-1")
        assert await asyncio.wait_for(q.get(), 1) == b"room-1"

        assert hub.set_talking(q, True) is True
        mic.put_nowait(b"room-2")
        await asyncio.sleep(0.05)
        assert q.empty(), "喊话时不应收到教室声音"

        assert hub.set_talking(q, False) is True
        mic.put_nowait(b"room-3")
        assert await asyncio.wait_for(q.get(), 1) == b"room-3"

        await hub.disconnect(q)

    asyncio.run(scenario())


def test_talking_state_change_is_reported_once() -> None:
    hub = AudioHub(AudioEngine())
    q: asyncio.Queue[bytes | str] = asyncio.Queue()
    assert hub.set_talking(q, True) is True
    assert hub.set_talking(q, True) is False, "重复按下不该再广播一次"
    assert hub.talking is True
    assert hub.set_talking(q, False) is True
    assert hub.talking is False


def test_disconnect_releases_talking_flag() -> None:
    """按住说话的客户端掉线，半双工门控必须自己解开。"""

    async def scenario() -> None:
        hub = AudioHub(AudioEngine())
        q = await hub.connect()
        hub.set_talking(q, True)
        await hub.disconnect(q)
        assert hub.talking is False
        assert hub._pump is None, "最后一个客户端走了应停掉麦克风泵"

    asyncio.run(scenario())


def test_broadcast_reaches_every_client() -> None:
    async def scenario() -> None:
        hub = AudioHub(AudioEngine())
        q1 = await hub.connect()
        q2 = await hub.connect()
        hub.broadcast({"type": "peer", "talking": True})

        for q in (q1, q2):
            msg = await asyncio.wait_for(q.get(), 1)
            assert isinstance(msg, str), "控制消息必须以文本帧下发"
            assert msg.startswith('{"type": "peer"')

        await hub.disconnect(q1)
        await hub.disconnect(q2)

    asyncio.run(scenario())


# --------------------------------------------------------------------- 设备生命周期


def test_device_opens_on_first_client_and_closes_on_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声卡按需开关：第一个进来才开，最后一个走了才关，中间不折腾。"""

    async def scenario() -> None:
        engine = AudioEngine()
        hub = AudioHub(engine)
        opened: list[str] = []
        closed: list[str] = []

        def fake_start() -> bool:
            opened.append("open")
            return True

        def fake_stop() -> None:
            closed.append("close")

        monkeypatch.setattr(engine, "start", fake_start)
        monkeypatch.setattr(engine, "stop", fake_stop)

        q1 = await hub.connect()
        assert opened == ["open"], "第一个客户端进来才该开设备"

        q2 = await hub.connect()
        assert opened == ["open"], "第二个客户端不该重复开设备"

        await hub.disconnect(q1)
        assert closed == [], "还有客户端在线，不该关设备"

        await hub.disconnect(q2)
        assert closed == ["close"], "最后一个客户端走了才该关设备"

        q3 = await hub.connect()
        assert opened == ["open", "open"], "全部断开后再有人来应重新开设备"
        await hub.disconnect(q3)
        assert closed == ["close", "close"]

    asyncio.run(scenario())


def test_client_still_connects_when_device_unavailable() -> None:
    """声卡开不了也要能连上，只是没有教室声音——不能让连拒绝掉。"""

    async def scenario() -> None:
        engine = AudioEngine()
        hub = AudioHub(engine)

        q = await hub.connect()
        assert hub.status()["clients"] == 1
        assert engine.available is False
        assert engine.error is not None, "降级原因要留痕，供 /api/status 展示"

        await hub.disconnect(q)
        assert hub.status()["clients"] == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------- 设备选择


def test_resolve_device_matches_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置存名字、开设备时解析成索引；方向不对或找不到就退回系统默认。"""
    fake = types.SimpleNamespace(
        query_devices=lambda: [
            {"name": "内置麦克风", "max_input_channels": 2, "max_output_channels": 0},
            {"name": "教室功放", "max_input_channels": 0, "max_output_channels": 2},
        ]
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    assert AudioEngine.resolve_device("内置麦克风", want_input=True) == 0
    assert AudioEngine.resolve_device("教室功放", want_input=False) == 1
    assert AudioEngine.resolve_device("内置麦克风", want_input=False) is None, "没有输出通道"
    assert AudioEngine.resolve_device("教室功放", want_input=True) is None, "没有输入通道"
    assert AudioEngine.resolve_device("不存在的设备", want_input=True) is None
    assert AudioEngine.resolve_device(None, want_input=True) is None, "没选就是系统默认"
    assert AudioEngine.resolve_device(3, want_input=True) == 3, "整数索引原样返回"


def test_resolve_device_without_sounddevice_returns_none() -> None:
    """声卡库都装不上时也要安静地退回默认，而不是抛异常。"""
    assert AudioEngine.resolve_device("任意设备", want_input=True) is None


def test_reconfigure_records_only_when_device_closed() -> None:
    engine = AudioEngine()
    assert engine.reconfigure("新麦克风", "新音响") is False
    assert engine.in_device == "新麦克风"
    assert engine.out_device == "新音响"


def test_reconfigure_reopens_when_device_open() -> None:
    engine = AudioEngine()
    engine.available = True
    engine.in_name = "旧麦克风"
    engine.play(pcm(*([1] * 10)))

    assert engine.reconfigure("新麦克风", None) is False  # 测试里声卡打不开
    assert engine.in_device == "新麦克风"
    assert engine.in_name is None, "stop() 应清掉旧设备名"
    assert engine.pending == 0, "换设备前要清空回放缓冲，别把旧声音放到新设备上"


def test_blocksize_tracks_sample_rate() -> None:
    """块大小按采样率换算，保证每块音频的时长不随采样率漂移。"""
    assert AudioEngine.blocksize_for(16000) == 1600
    assert AudioEngine.blocksize_for(48000) == 4800
    assert AudioEngine.blocksize_for(8000) == 800


def test_pick_rate_prefers_highest_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """设备只支持 44.1/48kHz 时要挑一个能用的，不能写死 16kHz。"""
    tried: list[float] = []

    def fake_check(**kwargs: float) -> None:
        tried.append(kwargs["samplerate"])
        if kwargs["samplerate"] not in (48000, 44100):
            raise RuntimeError("Invalid sample rate")

    fake = types.SimpleNamespace(
        check_input_settings=fake_check, check_output_settings=fake_check
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    assert AudioEngine.pick_rate(None, None) == 48000, "应挑最高的可用采样率"
    assert set(tried) == {48000}, "第一个就成功，不该继续试更低的采样率"


def test_pick_rate_skips_rates_only_one_side_supports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok(**kwargs: float) -> None:
        if kwargs["samplerate"] > 32000:
            raise RuntimeError("Invalid sample rate")

    fake = types.SimpleNamespace(check_input_settings=ok, check_output_settings=ok)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    assert AudioEngine.pick_rate(None, None) == 32000


def test_pick_rate_falls_back_when_nothing_works(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_fail(**kwargs: float) -> None:
        raise RuntimeError("nope")

    fake = types.SimpleNamespace(
        check_input_settings=always_fail, check_output_settings=always_fail
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    assert AudioEngine.pick_rate(None, None) == RATE


def test_pick_rate_without_sounddevice_returns_default() -> None:
    assert AudioEngine.pick_rate(None, None) == RATE


def test_status_exposes_selection_alongside_opened_device() -> None:
    engine = AudioEngine()
    engine.in_device = "USB 麦克风"
    status = engine.status()
    assert status["selected_in"] == "USB 麦克风", "选定的设备"
    assert status["device_in"] is None, "没打开就不该报设备名"
    assert status["engine_available"] is False


# --------------------------------------------------------------------- 状态


def test_status_reports_engine_details() -> None:
    engine = AudioEngine()
    status = engine.status()
    assert status["engine_available"] is False
    assert status["rate"] == 16000
    assert status["channels"] == 1
    assert status["subscribers"] == 0


def test_start_without_portaudio_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 PortAudio 时 start() 返回 False 并记下原因，而不是抛异常。"""
    engine = AudioEngine()
    # sys.modules 里的 None 会让 `import sounddevice` 抛 ImportError
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    assert engine.start() is False
    assert engine.available is False
    assert engine.error and "sounddevice 不可用" in engine.error


def test_stop_releases_device_and_clears_names() -> None:
    """释放后状态里不能再留着设备名，否则看起来像还开着。"""
    engine = AudioEngine()
    engine.available = True
    engine.in_name = "假麦克风"
    engine.out_name = "假音响"

    engine.stop()
    assert engine.available is False
    assert engine.in_name is None
    assert engine.out_name is None

    engine.stop()  # 幂等：没开过或已关掉都不该报错
    assert engine.available is False
