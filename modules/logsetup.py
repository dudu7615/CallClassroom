"""loguru 日志配置。

全项目统一用 loguru，不碰 `print`，也不直接用标准库 `logging` 打日志。
uvicorn 的访问日志仍然走标准库，所以这里挂一个 handler 把它转发进 loguru，
避免终端里出现两种格式。
"""

import inspect
import logging
import sys
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from types import FrameType

_FORMAT = (
    "<green>{time:YY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{name}.{function}:{line}</cyan> - "
    "<level>{message}</level>"
)

#: 这些 logger 的输出要转发给 loguru
_INTERCEPTED = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")


class _InterceptHandler(logging.Handler):
    """把标准库 logging 记录转给 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 跳过 logging 内部的调用帧，让日志里的模块名落在真正打日志的地方。
        # loguru 的 depth 与栈帧序号差 1：depth=1 就是 emit 自己，
        # 所以从"emit 的调用者"（帧号 1）起步时 depth 取 2，两者同步递增。
        current = inspect.currentframe()
        frame: FrameType | None = current.f_back if current is not None else None
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(*, verbose: bool = False) -> None:
    """安装 loguru 输出并接管 uvicorn 的日志。"""
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format=_FORMAT,
        # 不写死 colorize：终端里上色，重定向到文件时自动去掉 ANSI 码
        backtrace=verbose,
    )

    handler = _InterceptHandler()
    for name in _INTERCEPTED:
        std = logging.getLogger(name)
        std.handlers = [handler]
        std.propagate = False
    logging.basicConfig(handlers=[handler], level=0, force=True)
