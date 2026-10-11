"""在线备份：把当前数据库复制成一份一致的副本（SQLite 的 `backup` API）。

为什么不能用 `cp`：库处于 WAL 模式，最近的事务可能还在 `sqlite.db-wal` 里，
裸拷贝主文件会得到"缺最近写入"的副本。`sqlite3.Connection.backup()` 由 SQLite
自己保证一致性，而且**不需要停服** —— 读写并发下也能拿到一致快照。

边界：源库只读、目标文件只写，因此本模块**不参与** `write_tx()` 的跨进程写锁，
不与业务写入互相阻塞；反过来说，它也不该被塞进任何写事务里。

失败语义：任何中断（目标不可写、磁盘满、校验不过）都会**删掉半成品**并把错误
翻译成 `BackupError` —— 调用方（`run.py --backup`）据此打印一行原因并返回非零
退出码，而不是抛出一段 sqlite3 的 traceback。

调用点：`python run.py --backup`（运维入口，见 README 的"备份与恢复"）。
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from pathlib import Path

from . import connection

logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """备份无法完成（源库不存在、目标已存在、写入或校验失败）。"""


def backup_to(destination, *, db_path=None, verify: bool = True) -> Path:
    """把数据库备份到 `destination` 并返回目标路径（父目录自动创建）。

    - 源库优先以只读方式打开（`mode=ro`）；个别平台上只读连接读 WAL 需要写
      `-shm`，此时回落到普通连接（仍然只做 SELECT 级别的读）；
    - **拒绝覆盖已存在的文件**：备份的价值在于"上一份还在"，静默覆盖会让
      "备份成功但把好副本换成了坏副本"变得不可察觉；
    - 默认做一次 `PRAGMA quick_check`：宁可当场报错，也不要留一个打不开的
      "备份"。任何失败都会删掉半成品，并翻译成 `BackupError`。
    """
    source = Path(db_path) if db_path is not None else Path(connection.DB_PATH)
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise BackupError(f"refusing to overwrite an existing backup: {target}")

    try:
        _write_copy(source, target, verify=verify)
    except sqlite3.Error as exc:
        # 目标目录不可写、磁盘满、库损坏…… 统一变成可读的一句话
        _discard(target)
        raise BackupError(f"backup failed ({target}): {exc}") from exc
    except Exception:
        _discard(target)
        raise

    # 备份含用户 / 会话 / 登录流水：显式收紧到 0600，不依赖调用方的 umask
    # （systemd 的 UMask=0027 只会得到 0640，同组用户仍可读）。
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    logger.info("Database backed up: %s -> %s", source, target)
    return target


def _write_copy(source: Path, target: Path, *, verify: bool) -> None:
    """真正干活的部分：打开源库、写目标副本、可选校验。"""
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
    finally:
        src.close()


def _discard(target: Path) -> None:
    """删掉失败留下的半成品：它会挡住下一次备份（我们不覆盖已存在文件）。"""
    with contextlib.suppress(OSError):
        target.unlink()
