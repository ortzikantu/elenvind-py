"""Elenvind 持久化基座（唯一 SQLite 边界）。

    app（装配层）─┬─► db ──► SQLite
    modules ──────┘

铁律（由 tests/test_architecture.py 守卫）：

- **只有本包**可以 `import sqlite3`、执行 SQL、`BEGIN`/`COMMIT`；
- 所有写操作必须经 `transaction.write_tx()`（跨进程 flock + BEGIN IMMEDIATE）；
- 读操作走 `connection.connect()`（不参与 flock）；
- 本包**不认识** `core`、不认识 `modules`：不读配置、不处理 HTTP/Cookie/模板；
  数据库路径由装配层 `connection.configure()` 注入。

数据领域（按持久化对象组织，而不是按 HTTP 动作）：

| 模块 | 数据对象 |
|---|---|
| `connection` | 路径 / 锁文件 / 只读连接 |
| `transaction` | `write_tx()` 唯一写入口 |
| `migration` | schema 定义、`user_version` 迁移、`init_db()` |
| `user` | `user` 表 + 邮箱规范化 |
| `session` | `session` 表（绝对 + 滑动过期） |
| `comment` | `comment` 表（层级 / 树展开 / 涂黑） |
| `comment_rate` | 评论限流记账 + 原子写入 |
| `auth` | `login_attempts` / `register_attempts`（认证尝试流水） |
| `maintenance` | 机会式清理（独立写事务） |
| `backup` | 在线一致性备份（运维入口，只读源库） |

`modules` 可以 `from ...db import X` 或 `from ...db.comment import Y`；
装配层把本包**整个**作为 store 注入给 `core.session`（`store=db`）。
"""
from __future__ import annotations

from . import (
    auth,
    backup,
    comment,
    comment_rate,
    connection,
    maintenance,
    migration,
    session,
    transaction,
    user,
)
from .auth import (
    COOLDOWN_BASE_SECONDS,
    COOLDOWN_MAX_SECONDS,
    REGISTER_RETENTION_DAYS,
    RETENTION_DAYS,
    cleanup_old_attempts,
    cleanup_old_login_attempts,
    clear_login_attempts,
    complete_login_success,
    record_login_attempt,
    reserve_login_attempt,
    try_register_attempt,
)
from .backup import BackupError, backup_to
from .comment import (
    comment_depth,
    flatten_comment_tree,
    get_comment_by_id,
    get_comments_by_article,
    redact_comment,
    update_comment_content,
)
from .comment_rate import cleanup_old_comment_attempts, try_post_comment
from .connection import (
    DB_PATH,
    DEFAULT_DB_PATH,
    IntegrityError,
    configure,
    connect,
    get_connection,
    journal_mode,
    lock_path_for,
)
from .maintenance import prune
from .migration import SCHEMA_VERSION, MigrationError, init_db, migrate
from .session import (
    DEFAULT_ABSOLUTE_DAYS,
    DEFAULT_IDLE_DAYS,
    cleanup_expired_sessions,
    create_session,
    delete_session,
    delete_user_sessions,
    get_session_user,
)
from .transaction import write_tx
from .user import (
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_id,
    get_user_number,
    normalize_email,
    update_user_password,
    update_user_profile,
)

__all__ = [
    "COOLDOWN_BASE_SECONDS",
    "COOLDOWN_MAX_SECONDS",
    "DB_PATH",
    "DEFAULT_ABSOLUTE_DAYS",
    "DEFAULT_DB_PATH",
    "DEFAULT_IDLE_DAYS",
    "REGISTER_RETENTION_DAYS",
    "RETENTION_DAYS",
    "SCHEMA_VERSION",
    "BackupError",
    "IntegrityError",
    "MigrationError",
    "auth",
    "backup",
    "backup_to",
    "cleanup_expired_sessions",
    "cleanup_old_attempts",
    "cleanup_old_comment_attempts",
    "cleanup_old_login_attempts",
    "clear_login_attempts",
    "comment",
    "comment_depth",
    "comment_rate",
    "complete_login_success",
    "configure",
    "connect",
    "connection",
    "create_session",
    "create_user",
    "delete_session",
    "delete_user",
    "delete_user_sessions",
    "flatten_comment_tree",
    "get_comment_by_id",
    "get_comments_by_article",
    "get_connection",
    "get_session_user",
    "get_user_by_email",
    "get_user_by_id",
    "get_user_number",
    "init_db",
    "journal_mode",
    "lock_path_for",
    "maintenance",
    "migrate",
    "migration",
    "normalize_email",
    "prune",
    "record_login_attempt",
    "redact_comment",
    "reserve_login_attempt",
    "session",
    "transaction",
    "try_post_comment",
    "try_register_attempt",
    "update_comment_content",
    "update_user_password",
    "update_user_profile",
    "user",
    "write_tx",
]
