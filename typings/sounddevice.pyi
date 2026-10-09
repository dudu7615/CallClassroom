"""sounddevice 的类型桩（上游没有 py.typed）。

只声明本项目实际用到的部分：输入/输出流、设备查询。
"""

from collections.abc import Callable
from typing import Any, overload

type _Buffer = Any  # 回调收到的是 numpy 数组，细节由调用方按 dtype 断言
type _Callback = Callable[[_Buffer, int, Any, Any], None]
type _Device = int | str | None

class InputStream:
    device: int | str | None
    samplerate: float
    channels: int
    def __init__(
        self,
        *,
        samplerate: float | None = ...,
        channels: int | None = ...,
        dtype: str = ...,
        latency: float | str | None = ...,
        blocksize: int = ...,
        device: _Device = ...,
        callback: _Callback | None = ...,
        **kwargs: Any,
    ) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def abort(self) -> None: ...

class OutputStream:
    device: int | str | None
    samplerate: float
    channels: int
    def __init__(
        self,
        *,
        samplerate: float | None = ...,
        channels: int | None = ...,
        dtype: str = ...,
        latency: float | str | None = ...,
        blocksize: int = ...,
        device: _Device = ...,
        callback: _Callback | None = ...,
        **kwargs: Any,
    ) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def abort(self) -> None: ...

@overload
def query_devices() -> list[dict[str, Any]]: ...
@overload
def query_devices(device: _Device, kind: str | None = ...) -> dict[str, Any]: ...
def check_input_settings(
    *,
    device: _Device = ...,
    channels: int | None = ...,
    dtype: str | None = ...,
    samplerate: float | None = ...,
) -> None: ...
def check_output_settings(
    *,
    device: _Device = ...,
    channels: int | None = ...,
    dtype: str | None = ...,
    samplerate: float | None = ...,
) -> None: ...
def get_portaudio_version() -> tuple[int, str]: ...
def sleep(msec: float) -> None: ...
