"""在线备份：把当前数据库复制成一份一致的副本（SQLite 的 `backup` API）。

为什么不能用 `cp`：库处于 WAL 模式，最近的事务可能还在 `sqlite.db-wal` 里，
裸拷贝主文件会得到"缺最近写入"的副本。`sqlite3.Connection.backup()` 由 SQLite
自己保证一致性，而且**不需要停服** —— 读写并发下也能拿到一致快照。

边界：源库只读、目标文件只写，因此本模块**不参与** `write_tx()` 的跨进程写锁，
不与业务写入互相阻塞；反过来说，它也不该被塞进任何写事务里。

调用点：`python run.py --backup`（运维入口，见 README 的"备份与恢复"）。
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path

from . import connection

logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """备份无法完成（源库不存在、目标已存在、副本校验失败）。"""


def backup_to(destination, *, db_path=None, verify: bool = True) -> Path:
    """把数据库备份到 `destination` 并返回目标路径（父目录自动创建）。

    - 源库优先以只读方式打开（`mode=ro`）；个别平台上只读连接读 WAL 需要写
      `-shm`，此时回落到普通连接（仍然只做 SELECT 级别的读）；
    - **拒绝覆盖已存在的文件**：备份的价值在于"上一份还在"，静默覆盖会让
      "备份成功但把好副本换成了坏副本"变得不可察觉；
    - 默认做一次 `PRAGMA quick_check`：宁可当场报错，也不要留一个打不开的
      "备份"。任何失败都会删掉半成品。
    """
    source = Path(db_path) if db_path is not None else Path(connection.DB_PATH)
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise BackupError(f"refusing to overwrite an existing backup: {target}")

    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.OperationalError:      # pragma: no cover - 平台差异
        src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(target)
        try:
            with dst:
                src.backup(dst)
            if verify:
                row = dst.execute("PRAGMA quick_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    raise BackupError(f"backup verification failed: {row!r}")
        finally:
            dst.close()
    except Exception:
        # 半成品副本没有价值，而且会挡住下一次备份（我们不覆盖已存在文件）
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    finally:
        src.close()
    logger.info("Database backed up: %s -> %s", source, target)
    return target
