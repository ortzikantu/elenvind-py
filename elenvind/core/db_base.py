"""数据库引导：连接工厂、schema 初始化与版本迁移。

文件族说明：
- db_base.py         —— 唯一负责“打开连接 / 建表 / 建索引 / 迁移”的模块
- db_user.py         —— user 表相关查询与写入
- db_session.py      —— session 表（服务端会话）
- db_login.py        —— login_attempts 表（登录限流计数）
- db_register.py     —— register_attempts 表（注册限流计数）
- db_comment.py      —— comment 表（含软删除/恢复）
- db_comment_rate.py —— comment_rate 表（评论发布限流计数）

设计约定：
- 所有 SQL 一律使用占位符（?）传参，禁止字符串拼接用户输入（防 SQL 注入）。
- 每个函数自行开关连接：个人站规模下连接开销可忽略，换来“无共享状态、线程安全”的简单性。
  统一用 `with connect() as conn` 保证任何路径下都会提交/回滚并关闭。
- 每个连接都开启 `PRAGMA foreign_keys=ON`（SQLite 默认关闭，必须显式打开，
  否则外键约束形同虚设），并设置 WAL 与 busy_timeout。
- schema 版本用 `PRAGMA user_version` 记录，迁移在启动时自动执行（见 SCHEMA_VERSION）。
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

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
SCHEMA_VERSION = 2

# 连接级 PRAGMA：每个连接都必须设置（SQLite 没有全局开关）
_BUSY_TIMEOUT_MS = 5000


def get_connection(foreign_keys: bool = True):
    """获取一个已启用外键、WAL 与 busy timeout 的数据库连接。

    foreign_keys=False 仅供迁移过程使用（重建表时需要临时关闭约束检查）。

    注意：**必须**搭配 `with connect() as conn:` 使用，或自行 try/finally 关闭。
    直接 `with get_connection() as conn:` 是错的——sqlite3 的上下文管理器只负责
    提交/回滚事务，**不会关闭连接**（GC 时才关闭，表现为 ResourceWarning 与句柄泄漏）。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 行支持按列名取值：row["nickname"]
    # WAL 是持久化到数据库文件的属性，重复设置不会造成状态变化
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # 外键约束是"每连接"开关，必须显式打开
    conn.execute("PRAGMA foreign_keys=ON" if foreign_keys else "PRAGMA foreign_keys=OFF")
    return conn


@contextmanager
def connect(foreign_keys: bool = True):
    """连接的唯一推荐用法：成功提交、失败回滚、无论如何都关闭。

        with connect() as conn:
            conn.execute(...)

    这是 Core 里唯一允许打开连接的方式；Feature 不得直接调用 `sqlite3.connect`
    或 `get_connection`（见 tests/test_core_contract.py 的静态守卫）。
    """
    conn = get_connection(foreign_keys=foreign_keys)
    try:
        with conn:              # sqlite3 事务语义：正常提交，异常回滚
            yield conn
    finally:
        conn.close()            # 保证关闭：异常路径同样生效


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
    # 删号若走物理删除，会话随外键级联清理，不留悬空 token
    """
    CREATE TABLE IF NOT EXISTS session (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires REAL NOT NULL,
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


_MIGRATIONS = {
    2: _migrate_to_2,
}


def _run_migrations(conn):
    """在给定连接上执行缺失的迁移（调用方负责事务与关闭）。"""
    version = _read_version(conn)
    if version >= SCHEMA_VERSION:
        return version
    for target in range(version + 1, SCHEMA_VERSION + 1):
        migration = _MIGRATIONS.get(target)
        if migration is None:
            continue
        migration(conn)
        _write_version(conn, target)
        logger.info("Database migrated to schema version %s", target)
    return _read_version(conn)


def migrate():
    """把数据库结构升级到 SCHEMA_VERSION（幂等，可重复执行）。

    自行开连接、自行关闭；外键约束在迁移期间关闭（重建表需要在约束检查之外进行）。
    """
    with connect(foreign_keys=False) as conn:
        return _run_migrations(conn)


def init_db():
    """启动时调用：幂等地创建全部表与索引，然后执行 schema 迁移。"""
    apply_db_path()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        _create_schema(conn)
    migrate()
