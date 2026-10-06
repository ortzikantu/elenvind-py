"""Core startup/shutdown：启动初始化与关闭清理。

启动顺序固定（任一步失败 => 抛异常，服务拒绝启动，不会留下"半初始化"的应用）：

    config -> validate -> runtime config -> logging -> i18n -> templates
    -> database(+migration) -> cleanup -> content caches

log 目录、数据库目录在此阶段创建；路径统一相对项目根解析。
关闭时刷新并关闭全部日志 handler，避免日志丢失与句柄泄漏。

调用点（WSGI 只有"导入模块 + 调用 callable"两个时机，因此由入口模块显式调用）：

    elenvind/wsgi.py   导入时 startup(STARTUP_HOOKS)；atexit 注册 shutdown()
    run.py             启动前先做一次配置校验（失败即退出，不接管端口）
    tests/support.py   每个用例 startup(STARTUP_HOOKS) 一次

每个 gunicorn worker 都会执行一次 `startup()`（未开 `--preload` 时）；
init/migration 幂等，可安全重复执行。

内容缓存（文章 / 自定义页面）的预热钩子由装配层（`elenvind/app.py` 的
`STARTUP_HOOKS`）**显式传入** —— Core 不认识任何业务模块，也不持有
"模块注册表"这类全局可变状态。
"""
from __future__ import annotations

import logging
import os
import time

from .config import apply_runtime_config, config, load_config, validate_config
from .console import success, warning
from .db_base import DB_PATH, SCHEMA_VERSION, connect, init_db, lock_path_for
from .db_comment_rate import cleanup_old_comment_attempts
from .db_login import cleanup_old_login_attempts
from .db_register import cleanup_old_attempts
from .db_session import cleanup_expired_sessions
from .logging_config import resolve_log_path, setup_logging, shutdown_logging

logger = logging.getLogger(__name__)

#: 测试夹具开关：True 时跳过读取 config.toml，沿用调用方注入的 config 字典。
#: 生产路径永远为 False（不提供环境变量或配置开关）。
SKIP_CONFIG_LOAD = {"value": False}


def shutdown():
    """进程退出前清理：关闭全部日志 handler（flush 落盘 + 释放句柄）。

    由入口模块（`elenvind/wsgi.py` 的 atexit / gunicorn worker 退出）调用。
    幂等：`shutdown_logging()` 只是摘掉并关闭自己装的 handler。
    """
    warning("Application shutting down")
    shutdown_logging()


def startup(hooks=()):
    """按固定顺序完成全部启动步骤；异常向上抛，由调用方决定如何终止进程。

    参数 `hooks`：`[(说明, callable)]`，在数据库与模板就绪后**按序**执行
    （业务模块的内容缓存预热）。由装配层显式传入：钩子抛异常会使启动失败 ——
    这是刻意的：宁可拒绝启动，也不要跑一个"文章列表永远空的"站点。

    幂等地可重复调用（配置重读、日志重装、建表迁移、缓存预热都幂等），
    因此"每个 gunicorn worker 各跑一次"是安全的。

    运行期日志：每一步都留痕（INFO），并在末尾记一条"启动完成 + 耗时 + pid"，
    多 worker 时用来回答"哪个进程在什么时候起来的"。
    """
    started_at = time.monotonic()
    hooks = tuple(hooks)        # 可能被 len() 与 for 各用一次，先固化

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
    logger.info("Log file: %s (level=%s, rotate at %s bytes, keep %s file(s))",
                resolve_log_path(logging_cfg.get("file", "logs/app.log")),
                str(logging_cfg.get("level", "info")).lower(),
                logging_cfg.get("max_bytes", 10 * 1024 * 1024),
                logging_cfg.get("backup_count", 5))

    from . import i18n
    i18n.load()
    success("i18n messages loaded")

    # 模板目录/语法问题在此阶段暴露（不等到第一个页面请求）
    from .templating import get_environment
    get_environment()
    success("Templates loaded")

    init_db()
    success("Database initialized and migrated")

    # 运行期排障最常问的三个问题：库在哪、日志模式是什么、锁文件在哪 —— 直接记下来。
    with connect() as conn:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
    logger.info("Database: %s (schema v%s, journal_mode=%s, write lock %s)",
                DB_PATH, SCHEMA_VERSION, journal_mode, lock_path_for(DB_PATH))

    # 会话过期清理：与限流流水清理同一风格（启动时统一扫一遍）。
    # 运行期间读过期会话也会顺手删除，这里只是兜住"长期没被访问"的那些行。
    removed_sessions = cleanup_expired_sessions()
    cleanup_old_login_attempts(days=30)
    cleanup_old_comment_attempts(days=7)
    cleanup_old_attempts(days=7)
    success(f"Expired sessions and rate-limit history cleaned "
            f"({removed_sessions} session(s) removed)")

    for label, hook in hooks:
        hook()
        success(label)

    logger.info("Startup complete in %.0f ms (pid=%s, %d startup hook(s), locale=%s)",
                (time.monotonic() - started_at) * 1000.0,
                os.getpid(), len(hooks), config.get("locale", "en"))
