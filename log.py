"""统一日志（业务模块专用，不依赖 astrbot，保证可离线测试）。

用法：模块里 `log = get_logger("trade")`，然后 log.info/warning/exception。
main.py 在插件启动时调用 `bind_astrbot_logger()` 把 AstrBot 的 logger 接进来，
并调用 `setup_file_logging()` 在插件数据目录落一份可对账的轮转日志。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = "sharemarket"
LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARNING": logging.WARNING, "ERROR": logging.ERROR}

_FORMAT = "[股市] %(asctime)s %(levelname)-5s %(name)s | %(message)s"
_ROOT_LOGGER = logging.getLogger(ROOT)
if not any(isinstance(h, logging.NullHandler) for h in _ROOT_LOGGER.handlers):
    _ROOT_LOGGER.addHandler(logging.NullHandler())  # 未接 handler 时不吞不掉也不报警
_ROOT_LOGGER.propagate = False
_ROOT_LOGGER.setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}")


def set_level(level_name: str) -> None:
    _ROOT_LOGGER.setLevel(LOG_LEVELS.get(level_name.upper(), logging.INFO))


class _AstrBotHandler(logging.Handler):
    """把 sharemarket 的日志转发到 AstrBot 自己的 logger。"""

    _sharemarket_bridge = True

    def __init__(self, target):
        super().__init__()
        self._target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if record.levelno >= logging.ERROR:
                fn = getattr(self._target, "error", None)
            elif record.levelno >= logging.WARNING:
                fn = getattr(self._target, "warning", None) \
                    or getattr(self._target, "warn", None)
            else:
                fn = getattr(self._target, "info", None)
            if fn:
                fn(msg)
        except Exception:  # 日志本身绝不能抛
            pass


def bind_astrbot_logger(target) -> None:
    """把 AstrBot logger 作为 handler 挂上（重复调用只生效一次）。"""
    if any(getattr(h, "_sharemarket_bridge", False) for h in _ROOT_LOGGER.handlers):
        return
    h = _AstrBotHandler(target)
    h.setFormatter(logging.Formatter(_FORMAT))
    _ROOT_LOGGER.addHandler(h)


def setup_file_logging(log_dir: Path | str, level_name: str = "INFO") -> Path | None:
    """在数据目录落一份轮转日志（1MB × 5），用于事后对账排查。"""
    try:
        log_dir = Path(log_dir)   # 容忍 str 入参
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "sharemarket.log"
        for h in _ROOT_LOGGER.handlers:
            if isinstance(h, RotatingFileHandler) and \
                    Path(getattr(h, "baseFilename", "")) == path:
                return path
        h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s | %(message)s"))
        _ROOT_LOGGER.addHandler(h)
        return path
    except Exception as e:
        # 文件日志失败不影响插件运行，但必须留下痕迹：静默失败最难查
        import sys
        print(f"[sharemarket] 文件日志初始化失败：{type(e).__name__}: {e}",
              file=sys.stderr)
        return None
