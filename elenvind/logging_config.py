import logging
import logging.handlers
from pathlib import Path

def setup_logging(level: str = "info", log_file: str = "logs/app.log",
                  max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
    """配置根日志和 uvicorn 日志，输出到控制台与轮转文件"""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    log_level = level_map.get(level.lower(), logging.INFO)

    # 创建文件处理器
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(log_level)

    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 配置根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # 移除已有处理器，避免日志重复输出
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 为 uvicorn 及其访问日志配置相同处理器，并禁用传播以避免重复
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.setLevel(log_level)
        uvicorn_logger.propagate = False
        # 移除可能已存在的处理器，避免重复添加
        for handler in uvicorn_logger.handlers[:]:
            uvicorn_logger.removeHandler(handler)
        uvicorn_logger.addHandler(file_handler)
        uvicorn_logger.addHandler(console_handler)
