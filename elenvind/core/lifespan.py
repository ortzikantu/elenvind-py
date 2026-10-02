"""Core lifespan：启动初始化与关闭清理。

启动顺序固定（任一步失败 => lifespan.startup.failed，服务拒绝启动，
不会留下"半初始化"的应用）：

    config -> validate -> runtime config -> logging -> i18n -> templates
    -> database(+migration) -> cleanup -> content caches

log 目录、数据库目录在此阶段创建；路径统一相对项目根解析。
关闭时刷新并关闭全部日志 handler，避免日志丢失与句柄泄漏。

内容缓存（文章 / 自定义页面）通过 `register_startup_hook()` 注册，
因此 Core 不需要认识任何 Feature（保持依赖方向：Feature -> Core）。
"""
from __future__ import annotations

import logging

from .config import apply_runtime_config, config, load_config, validate_config
from .console import error, success, warning
from .db_base import init_db
from .db_comment_rate import cleanup_old_comment_attempts
from .db_login import cleanup_old_login_attempts
from .db_register import cleanup_old_attempts
from .db_session import cleanup_expired_sessions
from .logging_config import setup_logging, shutdown_logging

logger = logging.getLogger(__name__)

#: 测试夹具开关：True 时跳过读取 config.toml，沿用调用方注入的 config 字典。
#: 生产路径永远为 False（不提供环境变量或配置开关）。
SKIP_CONFIG_LOAD = {"value": False}

#: Feature 注册的启动钩子：[(说明, callable)]，在数据库就绪后按序执行
_STARTUP_HOOKS = []


def register_startup_hook(label: str, hook) -> None:
    """注册一个启动钩子（Feature 用它预热自己的内容缓存）。

    钩子抛异常会使启动失败——这是刻意的：宁可拒绝启动，
    也不要跑一个"文章列表永远空的"站点。
    """
    _STARTUP_HOOKS.append((label, hook))


def clear_startup_hooks() -> None:
    """清空启动钩子（测试隔离用）。"""
    _STARTUP_HOOKS.clear()




async def handle_lifespan(receive, send):
    """处理 ASGI lifespan 协议，负责启动初始化与关闭清理。"""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                startup()
            except Exception as e:      # noqa: BLE001 - 启动失败必须显式失败
                error(f"Startup failed: {e}")
                await send({"type": "lifespan.startup.failed", "message": str(e)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            warning("Application shutting down")
            shutdown_logging()
            await send({"type": "lifespan.shutdown.complete"})
            return


def startup():
    """按固定顺序完成全部启动步骤；异常向上抛，由 handle_lifespan 转为启动失败。"""
    if not SKIP_CONFIG_LOAD["value"]:
        load_config()
        success("Configuration loaded")
    else:
        success("Configuration provided by caller (config file load skipped)")

    validate_config()
    apply_runtime_config()
    success("Configuration validated")

    logging_cfg = config.get("logging", {})
    setup_logging(
        level=logging_cfg.get("level", "info"),
        log_file=logging_cfg.get("file", "logs/app.log"),
        max_bytes=logging_cfg.get("max_bytes", 10 * 1024 * 1024),
        backup_count=logging_cfg.get("backup_count", 5),
    )
    success("Logging configured")

    from . import i18n
    i18n.load()
    success("i18n messages loaded")

    # 模板目录/语法问题在此阶段暴露（不等到第一个页面请求）
    from .templating import get_environment
    get_environment()
    success("Templates loaded")

    init_db()
    success("Database initialized and migrated")

    # 会话过期清理：与限流流水清理同一风格（启动时统一扫一遍）。
    # 运行期间读过期会话也会顺手删除，这里只是兜住"长期没被访问"的那些行。
    removed_sessions = cleanup_expired_sessions()
    cleanup_old_login_attempts(days=30)
    cleanup_old_comment_attempts(days=7)
    cleanup_old_attempts(days=7)
    success(f"Expired sessions and rate-limit history cleaned "
            f"({removed_sessions} session(s) removed)")

    for label, hook in _STARTUP_HOOKS:
        hook()
        success(label)
