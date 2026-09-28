"""ASGI lifespan：启动初始化与关闭清理。

启动顺序固定（任一步失败 => lifespan.startup.failed，服务拒绝启动，
不会留下"半初始化"的应用）：
    config -> logging -> i18n -> database(+migration) -> cleanup
    -> article cache -> page cache

log 目录、数据库目录都会在此阶段创建；日志路径统一相对项目根目录解析。
关闭时刷新并关闭全部日志 handler，避免日志丢失与句柄泄漏。
"""
from .config import load_config, validate_config, apply_runtime_config, config
from .console import success, error, warning
from .db_base import init_db
from .db_session import cleanup_expired_sessions
from .db_login import cleanup_old_login_attempts
from .db_comment_rate import cleanup_old_comment_attempts
from .db_register import cleanup_old_attempts
from .logging_config import setup_logging, shutdown_logging
from .articles import load_articles
from .usrpages import load_pages
from . import i18n

# 测试夹具开关：True 时跳过读取 config.toml，沿用调用方注入的 config 字典。
# 生产路径永远为 False（这里不提供任何环境变量或配置开关）。
SKIP_CONFIG_LOAD = {"value": False}


async def handle_lifespan(receive, send):
    """处理 ASGI lifespan 协议，负责启动初始化与关闭清理"""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                _startup()
            except Exception as e:
                # 启动失败必须显式失败：uvicorn 会退出而不是跑一个残缺的站点
                error(f"Startup failed: {e}")
                await send({"type": "lifespan.startup.failed", "message": str(e)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            warning("Application shutting down")
            shutdown_logging()
            await send({"type": "lifespan.shutdown.complete"})
            return


def _startup():
    """按固定顺序完成全部启动步骤；异常向上抛出，由 handle_lifespan 转为启动失败。"""
    if not SKIP_CONFIG_LOAD["value"]:
        load_config()
        success("Configuration loaded")
    else:
        # 仅测试夹具使用：保留调用方已注入的 config 字典，不读磁盘上的 config.toml
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

    # 文案表：损坏/缺失文件在此阶段直接暴露（i18n.load 会抛异常）
    i18n.load()
    success("i18n messages loaded")

    init_db()
    success("Database initialized and migrated")

    cleanup_expired_sessions()
    cleanup_old_login_attempts(days=30)
    cleanup_old_comment_attempts(days=7)
    cleanup_old_attempts(days=7)
    success("Expired sessions and rate-limit history cleaned")

    load_articles()
    success("Articles loaded")

    load_pages()
    success("Custom pages loaded")
