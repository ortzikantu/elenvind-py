"""日志配置：控制台 + 轮转文件，根 logger 与 uvicorn logger 共用同一批 handler。

要点：
- 日志文件路径相对**项目根目录**解析（config.ROOT），与其它路径配置保持一致，
  不随启动时的 cwd 变化。
- 重复调用 setup_logging() 会先关闭并移除旧 handler，不会出现重复输出或句柄泄漏。
- uvicorn 的 logger 设 propagate=False 并使用同一批 handler，避免同一条日志打两遍。
- 关闭时由 shutdown_logging() 统一 flush + close（lifespan shutdown 阶段调用）。
"""
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import ROOT, resolve_path

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def resolve_log_path(log_file) -> Path:
    """日志文件路径：相对项目根目录；默认 logs/app.log。"""
    return resolve_path(log_file, ROOT / "logs" / "app.log")


def _remove_handlers(logger):
    """移除并关闭 logger 上已有的 handler（避免重复输出与文件句柄泄漏）。"""
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:      # pragma: no cover - close 失败不应阻断启动
            pass


def setup_logging(level: str = "info", log_file: str = "logs/app.log",
                  max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
    """配置根日志和 uvicorn 日志，输出到控制台与轮转文件。"""
    log_path = resolve_log_path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log_level = _LEVELS.get(str(level).lower(), logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    handlers = [file_handler, console_handler]

    root_logger = logging.getLogger()
    _remove_handlers(root_logger)
    root_logger.setLevel(log_level)
    for handler in handlers:
        root_logger.addHandler(handler)

    for logger_name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.setLevel(log_level)
        uvicorn_logger.propagate = False
        _remove_handlers(uvicorn_logger)
        for handler in handlers:
            uvicorn_logger.addHandler(handler)


def shutdown_logging():
    """关闭全部日志 handler（lifespan shutdown 时调用，确保日志落盘）。"""
    for logger_name in (None,) + _UVICORN_LOGGERS:
        logger = logging.getLogger() if logger_name is None else logging.getLogger(logger_name)
        _remove_handlers(logger)
