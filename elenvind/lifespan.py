from .config import load_config, config
from .console import success, error, warning
from .db_base import init_db
from .db_session import cleanup_expired_sessions
from .db_login import cleanup_old_login_attempts
from .db_comment_rate import cleanup_old_comment_attempts
from .logging_config import setup_logging
from .articles import load_articles
from .usrpages import load_pages
from . import i18n

async def handle_lifespan(receive, send):
    """处理 ASGI lifespan 协议，负责启动初始化与关闭清理"""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                load_config()
                logging_config = config.get("logging", {})
                setup_logging(
                    level=logging_config.get("level", "info"),
                    log_file=logging_config.get("file", "logs/app.log"),
                    max_bytes=logging_config.get("max_bytes", 10 * 1024 * 1024),
                    backup_count=logging_config.get("backup_count", 5),
                )
                success("Logging configured")
                i18n.load()
                success("i18n messages loaded")
                init_db()
                success("Database initialized")
                cleanup_expired_sessions()
                success("Configuration loaded successfully")
                cleanup_old_login_attempts(days=30)
                success("Old login attempts cleaned")
                cleanup_old_comment_attempts(days=7)
                success("Old comment attempts cleaned")
                load_articles()
                success("Articles loaded")
                load_pages()
                success("Custom pages loaded")
                await send({"type": "lifespan.startup.complete"})
            except Exception as e:
                error(f"Failed to load configuration: {e}")
                await send({"type": "lifespan.startup.failed", "message": str(e)})
                return
        elif message["type"] == "lifespan.shutdown":
            warning("Application shutting down")
            await send({"type": "lifespan.shutdown.complete"})
            return

