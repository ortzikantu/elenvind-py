"""日志配置：控制台 + 轮转文件，根 logger 与 gunicorn logger 共用同一批 handler。

要点：
- 日志文件路径相对**项目根目录**解析（config.ROOT），与其它路径配置保持一致，
  不随启动时的 cwd 变化。
- 重复调用 setup_logging() 会先关闭并移除旧 handler，不会出现重复输出或句柄泄漏。
- gunicorn 的 logger 设 propagate=False 并使用同一批 handler，避免同一条日志打两遍
  （gunicorn 的 error/access logger 默认自带 handler，且 propagate=True）。
- 关闭时由 shutdown_logging() 统一 flush + close（进程退出阶段调用，
  见 `elenvind/wsgi.py` 的 atexit）。
- 多 worker 下每个 worker 各开一份 RotatingFileHandler：并发轮转在极端情况下可能
  丢一行日志（gunicorn 自身也是这个行为），但绝不会写坏文件；日志不是数据。
"""
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import ROOT, resolve_path

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
#: gunicorn 自己的 logger（error=启动/worker 生命周期，access=访问日志）
_GUNICORN_LOGGERS = ("gunicorn", "gunicorn.error", "gunicorn.access")

#: `level = "debug"` 时会把真正的信息埋掉的第三方噪音源
#: （python-markdown 的 logger 名就是大写 `MARKDOWN`：每加载一个扩展打一条 DEBUG）。
_NOISY_LOGGERS = ("MARKDOWN", "markdown", "markdown.extensions", "jinja2")

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


def _quiet_noisy_loggers(level: int) -> None:
    """第三方库压到 INFO 以下不再输出 DEBUG。

    配 `level = "debug"` 是为了看**应用自己**的细节（写事务、锁、业务分支），
    而 python-markdown 每加载一个扩展就打一条 DEBUG，Jinja2 也一样 ——
    不压住的话真正的信息会被埋掉。只压这些已知噪音源：应用自己的
    `elenvind.*` 与 `gunicorn.*`（启动/worker 生命周期）仍跟随配置级别。
    """
    if level > logging.INFO:
        return
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)


def setup_logging(level: str = "info", log_file: str = "logs/app.log",
                  max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
    """配置根日志和 gunicorn 日志，输出到控制台与轮转文件。"""
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

    _quiet_noisy_loggers(log_level)

    for logger_name in _GUNICORN_LOGGERS:
        gunicorn_logger = logging.getLogger(logger_name)
        gunicorn_logger.setLevel(log_level)
        gunicorn_logger.propagate = False
        _remove_handlers(gunicorn_logger)
        for handler in handlers:
            gunicorn_logger.addHandler(handler)


def shutdown_logging():
    """关闭全部日志 handler（进程退出时调用，确保日志落盘）。"""
    for logger_name in (None,) + _GUNICORN_LOGGERS:
        logger = logging.getLogger() if logger_name is None else logging.getLogger(logger_name)
        _remove_handlers(logger)
