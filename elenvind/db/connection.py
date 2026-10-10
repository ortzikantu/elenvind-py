"""SQLite 连接层：数据库路径、跨进程写锁文件、只读连接入口。

边界（架构守卫检查）：
- 只有本包可以 `import sqlite3` / 执行 SQL；`core`、`modules` 都不允许；
- 数据库路径由**装配层**通过 `configure()` 注入（db 不认识 config / modules）；
- 写事务入口在 `transaction.write_tx()`，本模块只提供只读的 `connect()`。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

#: flock 只在 POSIX 上存在。目标部署环境是 Linux；非 POSIX 平台上写操作会
#: 明确失败，而不是偷偷退化成"进程内假锁"（那会静默丢掉跨进程保证）。
try:                                    # pragma: no cover - 平台分支
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None


#: 供业务层捕获的驱动异常：非 db 代码不该 import sqlite3，因此在这里重新导出。
IntegrityError = sqlite3.IntegrityError


def configure(db_path=None) -> Path:
    """装配层注入数据库路径（`None` = 回落到默认/环境变量路径）。

    以前这里会自己去读 `config.toml` —— 那让"持久化基座"反向依赖配置层。
    现在路径解析属于装配层（`elenvind/app.py`），db 只接受**已经解析好**的路径。
    """
    global DB_PATH
    DB_PATH = (Path(os.environ.get("ELENVIND_DB", str(DEFAULT_DB_PATH)))
               if db_path is None else Path(db_path))
    return DB_PATH


IntegrityError = sqlite3.IntegrityError


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "sqlite.db"


DB_PATH = Path(os.environ.get("ELENVIND_DB", str(DEFAULT_DB_PATH)))


_BUSY_TIMEOUT_MS = 5000


_WRITE_LOCK_SUFFIX = ".write.lock"


_SLOW_LOCK_WAIT_SECONDS = 1.0


def lock_path_for(db_path=None) -> Path:
    """从**当前有效**数据库路径派生的写锁文件路径。

        sqlite.db  ->  sqlite.db.write.lock

    每次调用都重新计算（不缓存），因此 `ELENVIND_DB` / `apply_db_path()` 切换
    数据库后，锁文件会跟着换 —— 全部 worker 对同一个数据库算出同一个锁路径。
    """
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    return path.with_name(path.name + _WRITE_LOCK_SUFFIX)


@contextmanager
def _write_file_lock():
    """跨进程排他锁：`fcntl.flock(LOCK_EX)`，阻塞等待，锁独立文件。

    要点：
    - **稳定路径**：锁文件只在需要时创建，之后**永不删除**（删除重建会让不同
      进程锁住不同 inode，互斥保证直接失效）；
    - 阻断式等待，不做轮询/重试/FIFO 队列/后台线程；
    - 释放靠内核锁语义 + 显式 `LOCK_UN`；进程被 SIGKILL 时由内核在 fd 关闭后释放。
    """
    if fcntl is None:                   # pragma: no cover - 平台分支
        raise RuntimeError(
            "C0 写协调依赖 fcntl.flock（POSIX）。当前平台不支持，"
            "拒绝在无跨进程保证的情况下执行写事务。")

    path = lock_path_for()
    # 锁文件所在目录必须存在（与数据库同目录；init_db() 也会建它）
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    started = time.monotonic()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)      # 阻塞直到拿到锁
        waited = time.monotonic() - started
        if waited >= _SLOW_LOCK_WAIT_SECONDS:
            logger.warning("Write lock wait %.0f ms (path=%s)",
                           waited * 1000, path)
        else:
            logger.debug("Write lock acquired in %.2f ms (path=%s)",
                         waited * 1000, path)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _configure_connection(conn, *, foreign_keys: bool, ensure_wal: bool):
    """连接级设置。**必须在 BEGIN 之前**执行。

    为什么：`PRAGMA journal_mode` 与 `PRAGMA foreign_keys` 在事务内是**空操作**
    （SQLite 明确要求没有 pending transaction 时才生效），所以它们只能在这里做。
    """
    conn.row_factory = sqlite3.Row  # 行支持按列名取值：row["nickname"]
    if ensure_wal:
        # WAL 是持久化的文件级属性：重复设置已是 WAL 的库是空操作。
        # 只在初始化/迁移路径（都在 write_tx 内）调用，普通连接不再切换 journal mode。
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # 外键约束是"每连接"开关，必须显式打开
    conn.execute("PRAGMA foreign_keys=ON" if foreign_keys else "PRAGMA foreign_keys=OFF")


def journal_mode() -> str:
    """当前 journal_mode（大写，如 `"WAL"`）—— 启动日志用，只读查询。

    放在 db 层而不是装配层：`core`/`app` 都不允许执行 SQL（架构守卫）。
    """
    with connect() as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()


def get_connection(foreign_keys: bool = True):
    """获取一个已启用外键与 busy timeout 的数据库连接。

    foreign_keys=False 仅供迁移过程使用（重建表时需要临时关闭约束检查）。

    注意：**必须**搭配 `with connect() as conn:` / `with write_tx() as conn:` 使用，
    或自行 try/finally 关闭。直接 `with get_connection() as conn:` 是错的——
    sqlite3 的上下文管理器只负责提交/回滚事务，**不会关闭连接**（GC 时才关闭，
    表现为 ResourceWarning 与句柄泄漏）。
    """
    conn = sqlite3.connect(DB_PATH)
    _configure_connection(conn, foreign_keys=foreign_keys, ensure_wal=False)
    return conn


@contextmanager
def connect(foreign_keys: bool = True):
    """**read path** 的连接入口：成功提交、失败回滚、无论如何都关闭。

        with connect() as conn:
            row = conn.execute("SELECT ...").fetchone()

    只用于只读查询（SELECT）与不含写操作的辅助查询。任何**写**都必须走
    `write_tx()`：它才有跨进程排他锁。"

    实现说明（别被"看起来像事务"骗到）：提交/回滚来自 `with conn:`，
    而 CPython 的 sqlite3 只为 DML 隐式开事务，**DDL 与 PRAGMA 不会** ——
    因此这里不是"把任意语句都变成原子事务"的屏障，这也是迁移必须显式
    `BEGIN IMMEDIATE`（由 write_tx() 统一提供）的原因。

    Core 里唯一允许打开连接的两个入口之一；模块不得直接调用
    `sqlite3.connect` 或 `get_connection`（见 tests/test_core_contract.py 守卫）。
    """
    conn = get_connection(foreign_keys=foreign_keys)
    try:
        with conn:              # sqlite3 事务语义：正常提交，异常回滚
            yield conn
    finally:
        conn.close()            # 保证关闭：异常路径同样生效
