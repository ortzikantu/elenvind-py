"""SQLite 写事务层：整个项目**唯一**的写入口 `write_tx()`。

    flock(LOCK_EX) → 连接 → PRAGMA → BEGIN IMMEDIATE → 业务 SQL
    → COMMIT / ROLLBACK → 关闭连接 → 释放 flock。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from . import connection
from .connection import _write_file_lock, get_connection

logger = logging.getLogger(__name__)


_SLOW_WRITE_SECONDS = 1.0


_write_depth = threading.local()


def _log_write_committed(conn, started: float, changes_before: int) -> None:
    """提交成功后记一行写事务明细（DEBUG；慢事务升级为 WARNING）。

    只记"改了多少行、花了多久、写的是哪个库"，不含任何业务数据 —— 既能排障，
    又不会把内容带进日志。想看每一笔写事务就把 `[logging].level` 设成 "debug"。
    """
    elapsed_ms = (time.monotonic() - started) * 1000.0
    changed = conn.total_changes - changes_before
    if elapsed_ms >= _SLOW_WRITE_SECONDS * 1000.0:
        logger.warning("Write transaction slow: %.0f ms, %d row(s) changed (path=%s)",
                       elapsed_ms, changed, connection.DB_PATH)
    else:
        logger.debug("Write transaction committed: %.1f ms, %d row(s) changed (path=%s)",
                     elapsed_ms, changed, connection.DB_PATH)


@contextmanager
def write_tx(*, foreign_keys: bool = True, ensure_wal: bool = False):
    """**整个项目唯一正式的 SQLite 写事务入口。**

        with write_tx() as conn:
            conn.execute("INSERT ...")

    生命周期严格如下（顺序就是保证本身）：

        acquire flock          <- 跨进程排他（独立锁文件）
            open connection    <- 连接级 PRAGMA（foreign_keys / busy_timeout [/ WAL]）
            BEGIN IMMEDIATE    <- 立刻取 SQLite 写锁，杜绝读写交错升级死锁
            yield conn         <- 业务 SQL 全部在锁内、在同一事务内
            COMMIT / ROLLBACK  <- 正常提交；异常回滚并原样重抛
            close connection
        release flock          <- 无论如何都释放

    设计要点：
    - **不接收 SQL**：它不是 executor，业务 SQL 仍写在各自的 db_*.py 里；
    - 异常路径绝不吞异常：业务异常原样抛出；提交失败同样抛出（不谎报成功），
      并尽力 ROLLBACK，然后忽略回滚自身的问题（原始异常优先）；
    - 资源释放全部走 try/finally：连接创建失败/BEGIN 失败/业务异常/提交异常
      都不会漏掉 close 与 unlock；
    - **禁止嵌套**：同一线程里再进一次 write_tx() 会立刻报错。flock 会在
      新的 fd 上自死锁，所以把"复合写事务调用了另一个写函数"这种错误
      变成显式异常，而不是挂死。
    - `foreign_keys` / `ensure_wal` 只给迁移与初始化用（重建表需关外键；
      WAL 只需在初始化时确立一次）。

    可观测性：拿到锁与提交成功各记一行；等锁超过 `_SLOW_LOCK_WAIT_SECONDS`
    或事务超过 `_SLOW_WRITE_SECONDS` 会升级为 WARNING（默认 INFO 级别下
    看不见逐笔 DEBUG 明细，把 `[logging].level` 设成 "debug" 即可）。
    """
    depth = getattr(_write_depth, "value", 0)
    if depth:
        raise RuntimeError(
            "write_tx() 不可嵌套：当前线程已在写事务中。"
            "复合写操作请放进同一个 with write_tx() 块，"
            "或在块外调用（嵌套会在 flock 上自死锁）。")
    _write_depth.value = depth + 1
    conn = None
    try:
        with _write_file_lock():
            conn = get_connection(foreign_keys=foreign_keys)
            try:
                # 连接级 PRAGMA 与 BEGIN IMMEDIATE 都在同一个 try 里：
                # 连"事务开始就失败"（例如 SQLite 写锁被协议外连接占着，
                # busy_timeout 到点报 database is locked）也必须关掉连接。
                if ensure_wal:
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                changes_before = conn.total_changes
                try:
                    yield conn
                    conn.commit()   # 提交失败会进 except：不得误报成功
                    _log_write_committed(conn, started, changes_before)
                except BaseException:
                    try:
                        conn.rollback()
                    except Exception:        # 原始异常优先，回滚失败只记日志
                        logger.exception("Rollback after an error in write_tx() failed")
                    logger.debug("Write transaction rolled back after %.1f ms (path=%s)",
                                 (time.monotonic() - started) * 1000.0, connection.DB_PATH)
                    raise
            finally:
                try:
                    conn.close()
                except Exception:        # 关闭失败不应覆盖业务异常
                    logger.exception("Closing the write connection failed")
    finally:
        _write_depth.value = depth
