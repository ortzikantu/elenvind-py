"""数据库引导：连接工厂、**唯一写事务入口** `write_tx()`、schema 初始化与版本迁移。

文件族说明：
- db_base.py         —— 唯一负责“打开连接 / 写事务 / 建表 / 建索引 / 迁移”的模块
- db_user.py         —— user 表相关查询与写入
- db_session.py      —— session 表（服务端会话）
- db_login.py        —— login_attempts 表（登录限流计数）
- db_register.py     —— register_attempts 表（注册限流计数）
- db_comment.py      —— comment 表（含软删除/恢复）
- db_comment_rate.py —— comment_rate 表（评论发布限流计数）
- db_prune.py        —— 限流流水的机会式清理

## 两个入口，职责分开（C0 写协调）

    connect()     只用于 read path：SELECT，不加锁，简单直接
    write_tx()    **所有**写事务的唯一入口：跨进程排他锁 + BEGIN IMMEDIATE + 提交/回滚

    模块 ──► Core 业务 DB API（db_user / db_comment / …）
                    ├── 读 ──► connect()
                    └── 写 ──► write_tx() ──► flock(独立锁文件) ──► SQLite

写协调为什么需要**两层**：

1. `flock`（跨进程）：Gunicorn 多 worker 之间互斥。它管的是"同一时刻只有一个
   应用进程在写"，锁覆盖**整个** SQLite 事务（从 BEGIN IMMEDIATE 到 COMMIT/ROLLBACK）。
2. SQLite 事务（`BEGIN IMMEDIATE` → COMMIT/ROLLBACK）：事务原子性与一致性的
   最终权威。即使有人绕过 flock（外部脚本、别的工具），SQLite 自身的锁与
   `busy_timeout` 仍然是最后一道兜底 —— 但**不要**把 busy_timeout 当成写协调的替代品。

锁文件与数据库文件**分开**：`sqlite.db` → `sqlite.db.write.lock`，路径从当前
`DB_PATH` 动态派生（每次调用重新计算，不缓存）。绝不 `flock` 数据库本体。

适用范围与限制（详见 docs/development/modules.md）：
- 互斥只覆盖**遵守本协议**的进程；绕过 `write_tx()` 的程序不会自动遵守应用锁；
- `flock` 不提供严格 FIFO：竞争进程只是阻塞等待，不保证先来先得；
- 数据库与锁文件必须位于**本地文件系统**，不支持多机共享（NFS 等不在方案内）；
- WAL 允许读写并发，但不代表没有 checkpoint 相关阻塞。

## 其它约定

- 所有 SQL 一律使用占位符（?）传参，禁止字符串拼接用户输入（防 SQL 注入）。
- 每个函数自行开关连接：个人站规模下连接开销可忽略，换来“无共享状态、线程安全”的简单性。
- 每个连接都开启 `PRAGMA foreign_keys=ON`（SQLite 默认关闭，必须显式打开，
  否则外键约束形同虚设），并设置 busy_timeout。
- WAL 是**数据库文件级**的持久属性：由初始化/迁移路径（`init_db()`/`migrate()`，
  都在 `write_tx()` 内）确立一次，普通连接不再反复切换 journal mode。
- schema 版本用 `PRAGMA user_version` 记录，迁移在启动时自动执行（见 SCHEMA_VERSION）。
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

#: 供业务模块使用的异常类型：模块不该 import sqlite3（见 Core Contract 守卫），
#: 因此由 Core 重新导出它需要捕获的驱动异常。
IntegrityError = sqlite3.IntegrityError


class MigrationError(RuntimeError):
    """迁移无法安全继续（例如上次迁移留下的半成品备份表）。

    启动阶段抛出它会中止进程（启动失败），这是刻意的：
    宁可拒绝启动让你去看一眼数据库，也不要静默把数据搁置在 `*_legacy` 表里。
    """


# 默认数据库位于项目根目录；可用 ELENVIND_DB 环境变量覆盖（部署隔离 / 自动化测试用）
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "sqlite.db"
DB_PATH = Path(os.environ.get("ELENVIND_DB", str(DEFAULT_DB_PATH)))


def apply_db_path():
    """把 config.toml 的 database 键（相对项目根）应用到 DB_PATH。

    优先级（与 docs/CONFIGURATION.md 一致）：
        ELENVIND_DB 环境变量  >  config.toml 的 database  >  默认 <项目根>/sqlite.db

    配置为空时**显式回落到默认路径**，而不是保留当前值——否则空配置会让
    "数据库在哪"取决于进程之前碰过什么，属于隐式状态。
    """
    global DB_PATH
    from .config import config, resolve_path

    if os.environ.get("ELENVIND_DB"):
        DB_PATH = Path(os.environ["ELENVIND_DB"])
        return DB_PATH
    configured = config.get("database")
    if isinstance(configured, str) and configured.strip():
        DB_PATH = resolve_path(configured, DEFAULT_DB_PATH)
    else:
        DB_PATH = DEFAULT_DB_PATH
    return DB_PATH


# schema 版本：每次结构变更 +1，并在 _MIGRATIONS 登记迁移函数
SCHEMA_VERSION = 4

# 连接级 PRAGMA：每个连接都必须设置（SQLite 没有全局开关）
_BUSY_TIMEOUT_MS = 5000

#: 写锁文件的后缀（与数据库文件同目录、不同文件）
_WRITE_LOCK_SUFFIX = ".write.lock"

#: 等待写锁超过这个时长就打 WARNING（可观测性：回答"C0 是否已成为瓶颈"）
_SLOW_LOCK_WAIT_SECONDS = 1.0

#: 单个写事务（BEGIN 到 COMMIT）超过这个时长就打 WARNING。正常写都是毫秒级，
#: 超过 1 秒通常意味着慢查询、锁竞争或磁盘问题。
_SLOW_WRITE_SECONDS = 1.0

#: 写事务重入守卫（每线程）：同一线程嵌套 write_tx() 会在 flock 上自死锁
_write_depth = threading.local()

#: flock 只在 POSIX 上存在。目标部署环境是 Linux；非 POSIX 平台上写操作会
#: 明确失败，而不是偷偷退化成"进程内假锁"（那会静默丢掉跨进程保证）。
try:                                    # pragma: no cover - 平台分支
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None


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


def _log_write_committed(conn, started: float, changes_before: int) -> None:
    """提交成功后记一行写事务明细（DEBUG；慢事务升级为 WARNING）。

    只记"改了多少行、花了多久、写的是哪个库"，不含任何业务数据 —— 既能排障，
    又不会把内容带进日志。想看每一笔写事务就把 `[logging].level` 设成 "debug"。
    """
    elapsed_ms = (time.monotonic() - started) * 1000.0
    changed = conn.total_changes - changes_before
    if elapsed_ms >= _SLOW_WRITE_SECONDS * 1000.0:
        logger.warning("Write transaction slow: %.0f ms, %d row(s) changed (path=%s)",
                       elapsed_ms, changed, DB_PATH)
    else:
        logger.debug("Write transaction committed: %.1f ms, %d row(s) changed (path=%s)",
                     elapsed_ms, changed, DB_PATH)


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
                    except Exception:    # noqa: BLE001 - 原始异常优先
                        logger.exception("Rollback after an error in write_tx() failed")
                    logger.debug("Write transaction rolled back after %.1f ms (path=%s)",
                                 (time.monotonic() - started) * 1000.0, DB_PATH)
                    raise
            finally:
                try:
                    conn.close()
                except Exception:        # noqa: BLE001 - 关闭失败不应覆盖业务异常
                    logger.exception("Closing the write connection failed")
    finally:
        _write_depth.value = depth



# ======================= 建表（新库 / 缺表补齐） =======================

_SCHEMA_STATEMENTS = (
    # 用户表：注册即写入；删除走 is_deleted 逻辑删除，保留评论归属
    """
    CREATE TABLE IF NOT EXISTS user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nickname TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_deleted INTEGER NOT NULL DEFAULT 0,
        nickname_changed_at TEXT
    )
    """,
    # 会话表：登录颁发随机 token 存入，注销/过期即删（服务端会话，无 JWT）
    # - created_at：创建时刻，用于**绝对过期**（活多久都必须重新登录）
    # - last_seen ：最近活跃时刻，用于**滑动过期**（闲置太久即失效）
    # 两个阈值都在 config.toml 里配置（0 = 该维度不过期）。
    # 删号若走物理删除，会话随外键级联清理，不留悬空 token。
    """
    CREATE TABLE IF NOT EXISTS session (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE
    )
    """,
    # 登录尝试流水：只用于失败限流统计，定期清理；按邮箱/IP 维度各建索引
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        ip TEXT NOT NULL,
        attempted_at REAL NOT NULL,
        success INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts(email COLLATE NOCASE, attempted_at)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at)",
    # 注册尝试流水：公开注册的 IP 维度限流
    """
    CREATE TABLE IF NOT EXISTS register_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        attempted_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_register_attempts_ip ON register_attempts(ip, attempted_at)",
    # 评论发布流水：短窗口限流（防刷屏），只记 (user_id, ip, 时间)
    """
    CREATE TABLE IF NOT EXISTS comment_rate (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ip TEXT NOT NULL,
        attempted_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comment_rate_user ON comment_rate(user_id, attempted_at)",
    "CREATE INDEX IF NOT EXISTS idx_comment_rate_ip ON comment_rate(ip, attempted_at)",
    # 评论表：删除为软删除（is_deleted=1），parent_id 指向父评论形成楼中楼。
    # parent_id 使用 ON DELETE SET NULL：父评论被物理清除时子评论升级为顶层，
    # 既不会级联删掉一整棵树，也不会留下指向不存在行的孤儿评论。
    """
    CREATE TABLE IF NOT EXISTS comment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        parent_id INTEGER,
        is_deleted INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES user(id),
        FOREIGN KEY(parent_id) REFERENCES comment(id) ON DELETE SET NULL
    )
    """,
    # 文章页评论按文章聚合读取，给 (article_slug) 建索引
    "CREATE INDEX IF NOT EXISTS idx_comment_article ON comment(article_slug)",
    # 会话按用户删除（每次登录的轮换都会跑），必须走索引而不是全表扫描。
    # 放在 _SCHEMA_STATEMENTS 里，新建库直接就有；已有库由 v4 迁移补上。
    "CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id)",
)


def _create_schema(conn):
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


# ======================= 迁移 =======================

def _read_version(conn) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _write_version(conn, version: int):
    # PRAGMA 不支持参数占位符；version 来自本模块常量，非用户输入
    conn.execute(f"PRAGMA user_version={int(version)}")


def _table_sql(conn, table: str):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row["sql"] if row else None


def _rebuild_comment_table(conn):
    """重建 comment 表，把 parent_id 约束改为 ON DELETE SET NULL 并清理孤儿行。

    步骤：建新表 -> 复制数据（悬空 parent_id 归零）-> 换名。全程在原连接内完成，
    调用方负责关闭外键约束（重建期间不检查引用完整性）。
    """
    conn.execute("ALTER TABLE comment RENAME TO comment_legacy")
    conn.execute("""
        CREATE TABLE comment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_slug TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            parent_id INTEGER,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES user(id),
            FOREIGN KEY(parent_id) REFERENCES comment(id) ON DELETE SET NULL
        )
    """)
    conn.execute("""
        INSERT INTO comment (id, article_slug, user_id, content, created_at, parent_id, is_deleted)
        SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at,
               CASE WHEN p.id IS NULL THEN NULL ELSE c.parent_id END,
               c.is_deleted
        FROM comment_legacy c
        LEFT JOIN comment_legacy p ON p.id = c.parent_id
    """)
    conn.execute("DROP TABLE comment_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_article ON comment(article_slug)")


def _repair_orphan_comments(conn):
    """把指向不存在父评论的 parent_id 归零（浏览器侧等同升级为顶层评论）。"""
    conn.execute(
        "UPDATE comment SET parent_id = NULL "
        "WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM comment)"
    )


def _migrate_to_2(conn):
    """v2：comment.parent_id 改为 ON DELETE SET NULL；修复历史孤儿评论。

    幂等：表定义已是目标形态时只清理孤儿行，不做无意义重建。
    """
    sql = _table_sql(conn, "comment") or ""
    if "ON DELETE SET NULL" not in sql.upper():
        logger.info("Migrating schema to v2: rebuilding comment table")
        _rebuild_comment_table(conn)
    _repair_orphan_comments(conn)


#: 迁移旧 session 表时用的历史会话时长（天）。
#: 旧实现写死 7 天（`expires = 创建时刻 + 7 天`）。v3 需要把 `expires`
#: 反推回创建时刻，所以必须用**当时的**数值，而不是现在的配置值——
#: 否则会凭空延长或缩短已有会话的绝对寿命。
_LEGACY_SESSION_DAYS = 7


def _migrate_to_3(conn):
    """v3：session 表由单个 `expires` 改为 created_at + last_seen。

    绝对过期与滑动过期需要两个不同时间戳，单个 `expires` 表达不了。
    旧行按"最保守"方式回填（不会延长任何已有会话的寿命）：

        created_at = expires - 7 天     （等价于保留原来的绝对到期时刻）
        last_seen  = created_at         （旧实现里只要被读取就会续期，
                                          但无从得知真实活跃时刻，
                                          因此按"从未活跃"处理）

    即：老会话要么在原 expires 到期，要么在滑动窗口到期，取先到者——
    绝不比迁移前活得更久。

    幂等：表里已有 created_at 时只做一次兜底回填。
    """
    sql = _table_sql(conn, "session") or ""
    if "created_at" not in sql or "last_seen" not in sql:
        logger.info("Migrating schema to v3: rebuilding session table")
        conn.execute("ALTER TABLE session RENAME TO session_legacy")
        conn.execute("""
            CREATE TABLE session (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE
            )
        """)
        if "expires" in (sql or ""):
            legacy_span = _LEGACY_SESSION_DAYS * 86400
            conn.execute("""
                INSERT INTO session (token, user_id, created_at, last_seen)
                SELECT token, user_id,
                       expires - ?,
                       expires - ?
                FROM session_legacy
            """, (legacy_span, legacy_span))
        else:
            # 结构未知时宁可丢弃会话（让用户重新登录），也不要留下无时间戳的行
            logger.warning("Legacy session table has no expires column; "
                           "dropping %s session rows", _table_row_count(conn,
                                                                        "session_legacy"))
        conn.execute("DROP TABLE session_legacy")
    else:
        # 已是目标结构：补齐历史迁移可能留下的 NULL/非法值
        conn.execute("UPDATE session SET last_seen = created_at "
                     "WHERE last_seen IS NULL OR last_seen < created_at")


def _table_row_count(conn, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:                                  # noqa: BLE001
        return 0


def _migrate_to_4(conn):
    """v4：补齐会话索引，并让邮箱查询能走索引。

    两个性能问题（都是全表扫描，而其中一条每次登录都跑）：

    1. `session.user_id` 没有索引 —— `delete_user_sessions()` 是
       `DELETE FROM session WHERE user_id = ?`，每次登录轮换都会全表扫描。

    2. `user.email` 的隐式 UNIQUE 索引是 **BINARY** 排序规则，而查询写的是
       `WHERE email = ? COLLATE NOCASE` —— 排序规则不匹配，索引用不上，
       于是**每次登录/注册/改邮箱都全表扫描 user 表**。
       修法是让数据本身就规范化：写入侧（`normalize_email`）一直转小写，
       迁移这里把历史遗留的大写邮箱也转小写，之后查询用 BINARY 比较即可命中索引。

    幂等：索引用 IF NOT EXISTS；邮箱只有当真的存在非小写行时才更新。
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id)")

    # GLOB 是大小写敏感的，因此这条能精确找出"含非小写字符"的邮箱。
    # 加 ASCII 范围判断是防御性的：normalize_email 只保证 strip+lower，
    # 不会把非 ASCII 大写字母降为小写（Python 的 lower() 其实会，
    # 但库里若有异常数据，宁可跳过也不要用 SQL 的 lower() 悄悄改坏它）。
    uppercase = conn.execute(
        "SELECT COUNT(*) FROM user "
        "WHERE email GLOB '*[A-Z]*' AND email NOT GLOB '*[^ -~]*'"
    ).fetchone()[0]
    if uppercase:
        logger.info("Migrating schema to v4: normalizing %s email(s) to lowercase",
                    uppercase)
        conn.execute(
            "UPDATE user SET email = lower(email) "
            "WHERE email GLOB '*[A-Z]*' AND email NOT GLOB '*[^ -~]*'")
    else:
        logger.info("Migrating schema to v4: emails already normalized")


_MIGRATIONS = {
    2: _migrate_to_2,
    3: _migrate_to_3,
    4: _migrate_to_4,
}


def _leftover_rebuild_tables(conn):
    """列出上次迁移失败留下的 `*_legacy` 备份表。

    这些表的存在意味着某个表重建（RENAME -> CREATE -> INSERT..SELECT -> DROP）
    只做了一半。绝不能当作"已迁移"继续往下走——那会把备份表里仅存的
    数据永久搁置（目标表是空的，而 user_version 会被写成最新版）。
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%\\_legacy' "
        "ESCAPE '\\'").fetchall()
    return sorted(row[0] for row in rows)


def _run_migrations(conn):
    """在给定连接上执行缺失的迁移；**必须在 `write_tx()` 的事务内调用**。

    事务由 `write_tx()` 负责：它先取跨进程 flock，再 `BEGIN IMMEDIATE`，然后才
    yield 出这条连接。因此这里不再自己 BEGIN/COMMIT/ROLLBACK —— 迁移里的 DDL
    （`ALTER TABLE ... RENAME` / `CREATE TABLE` / `DROP TABLE`）与其后的数据搬运
    处于**同一个** SQLite 事务，任一步失败都会整体回滚。

    为什么显式事务是必须的（也是它必须由 write_tx 提供的原因）：CPython 的
    sqlite3 只为 INSERT/UPDATE/DELETE/REPLACE 开隐式事务，**DDL 与 PRAGMA 不会**。
    没有显式 BEGIN 时 `ALTER TABLE` 会被立即提交 —— 一旦后续步骤失败/进程被杀，
    就留下"备份表有数据、目标表为空"的半迁移态，而迁移逻辑靠子串匹配判断
    "已迁移"，会把这个状态误认为成功。

    先做一次残留检查：发现 `*_legacy` 就抛错拒绝启动，
    由运维决定是恢复备份还是丢弃（绝不静默继续）。
    """
    leftovers = _leftover_rebuild_tables(conn)
    if leftovers:
        raise MigrationError(
            "检测到上次迁移未完成，以下备份表仍存在：" + ", ".join(leftovers)
            + "。请先用 sqlite3 检查并恢复数据（备份表里通常还有完整数据），"
              "确认后再手工删除这些表重启。为避免数据丢失，迁移已中止。")

    version = _read_version(conn)
    if version >= SCHEMA_VERSION:
        return version

    try:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            migration = _MIGRATIONS.get(target)
            if migration is None:
                continue
            migration(conn)
            _write_version(conn, target)
            logger.info("Database migrated to schema version %s", target)
    except Exception:
        # 回滚由 write_tx() 负责（这里只记录运维需要看到的原因）
        logger.exception("Migration failed; schema rolled back to version %s", version)
        raise
    return _read_version(conn)


def migrate():
    """把数据库结构升级到 SCHEMA_VERSION（幂等，可重复执行）。

    全程在 C0 写协调边界内（flock + BEGIN IMMEDIATE），因此多个 Gunicorn worker
    同时启动时不会并发迁移：排在后面的 worker 进来时 `user_version` 已是目标版本，
    直接返回（幂等），不会重复执行任何迁移步骤。

    外键约束在迁移期间关闭（重建表的 RENAME/DROP 需要在约束检查之外进行）；
    注意 `PRAGMA foreign_keys` 在事务内是空操作，所以它必须在 BEGIN 之前设置 ——
    由 `get_connection()` 在 `_write_file_lock()` 内、BEGIN 之前完成。

    另外确立 WAL（文件级持久属性）：这是初始化路径的职责，普通连接不再切换
    journal mode。
    """
    with write_tx(foreign_keys=False, ensure_wal=True) as conn:
        return _run_migrations(conn)


def init_db():
    """启动时调用：幂等地创建全部表与索引，然后执行 schema 迁移。

    注意顺序：先 `_create_schema`（`CREATE TABLE IF NOT EXISTS`）再迁移。
    对**全新**数据库这是对的；对已有库，`_create_schema` 不会改已有表，
    而残留检查会拦住半迁移态，因此不会出现"空表被误认为已迁移"。

    整段（残留检查 + 建表 + 迁移）在**同一个** `write_tx()` 里完成：
    Gunicorn 多 worker 各自在导入时调用它，flock 让它们串行 —— 第一个 worker
    建表迁移，其余 worker 进来时发现版本已是最新，什么都不做。

    顺带修正了一处历史差异：以前 `_create_schema` 走的是普通 `connect()`，
    而 DDL 不会隐式开事务（`CREATE TABLE` 立即提交）；现在它在
    `BEGIN IMMEDIATE` 之后执行，建表失败会整体回滚（不再留下"建了一半的表"）。
    """
    apply_db_path()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with write_tx(foreign_keys=False, ensure_wal=True) as conn:
        leftovers = _leftover_rebuild_tables(conn)
        if leftovers:
            raise MigrationError(
                "检测到上次迁移未完成，备份表仍存在：" + ", ".join(leftovers)
                + "。请先恢复数据再启动（详见 _run_migrations 的说明）。")
        _create_schema(conn)
        _run_migrations(conn)
