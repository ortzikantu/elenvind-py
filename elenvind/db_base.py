"""数据库引导：连接工厂与 schema 初始化。

文件族说明：
- db_base.py         —— 唯一负责“打开连接 / 建表 / 建索引”的模块
- db_user.py         —— user 表相关查询与写入
- db_session.py      —— session 表（服务端会话）
- db_login.py        —— login_attempts 表（登录限流计数）
- db_comment.py      —— comment 表（含软删除/恢复）
- db_comment_rate.py —— comment_rate 表（评论发布限流计数）

设计约定：
- 所有 SQL 一律使用占位符（?）传参，禁止字符串拼接用户输入（防 SQL 注入）。
- 每个函数自行开关连接：个人站规模下连接开销可忽略，换来“无共享状态、线程安全”的简单性。
- SQLite 连接开启 WAL 模式，允许读与写并发；busy_timeout 缓解写锁竞争。
"""
import os
import sqlite3
from pathlib import Path

# 默认数据库位于项目根目录；可用 ELENVIND_DB 环境变量覆盖（部署隔离 / 自动化测试用）
DB_PATH = Path(os.environ.get("ELENVIND_DB", str(Path(__file__).resolve().parent.parent / "sqlite.db")))


def get_connection():
    """获取一个已启用 WAL 与 busy timeout 的数据库连接。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 行支持按列名取值：row["nickname"]
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """启动时调用：幂等地创建全部表与索引（IF NOT EXISTS），兼容已有旧库。"""
    conn = get_connection()
    try:
        # 用户表：注册即写入；删除走 is_deleted 逻辑删除，保留评论归属
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                nickname_changed_at TEXT
            )
        """)
        # 会话表：登录颁发随机 token 存入，注销/过期即删（服务端会话，无 JWT）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user(id)
            )
        """)
        # 登录尝试流水：只用于失败限流统计，定期清理；按邮箱/IP 维度各建索引
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                ip TEXT NOT NULL,
                attempted_at REAL NOT NULL,
                success INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts(email COLLATE NOCASE, attempted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at)")
        # 评论发布流水：短窗口限流（防刷屏），只记 (user_id, ip, 时间)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comment_rate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ip TEXT NOT NULL,
                attempted_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_rate_user ON comment_rate(user_id, attempted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_rate_ip ON comment_rate(ip, attempted_at)")
        # 评论表：删除为软删除（is_deleted=1），parent_id 指向父评论形成楼中楼
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                parent_id INTEGER,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES user(id)
            )
        """)
        # 文章页评论按文章聚合读取，给 (article_slug) 建索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_article ON comment(article_slug)")
        conn.commit()
    finally:
        conn.close()
