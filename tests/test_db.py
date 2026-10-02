"""数据库层测试：外键、schema 迁移、连接生命周期、约束、限流流水与数据完整性。"""
import os
import sqlite3
import unittest
from pathlib import Path

from tests.support import ElenvindTestCase

from elenvind.core import db_base
from elenvind.core.db_base import SCHEMA_VERSION, connect, init_db, migrate
from elenvind.core.db_comment import create_comment, get_comment_by_id, get_comments_by_article
from elenvind.core.db_login import (
    cleanup_old_login_attempts,
    count_email_failures,
    count_ip_failures,
    record_login_attempt,
)
from elenvind.core.db_register import try_register_attempt
from elenvind.core.db_session import create_session, delete_user_sessions, get_session_user
from elenvind.core.db_user import (
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_id,
    get_user_number,
    update_user_password,
    update_user_profile,
)
from elenvind.core.security import hash_password


class ConnectionTests(ElenvindTestCase):
    def test_foreign_keys_enabled_on_every_connection(self):
        for _ in range(3):
            with connect() as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_wal_mode_and_busy_timeout(self):
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertGreaterEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_row_factory_is_row(self):
        with connect() as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)

    def test_foreign_key_violation_is_rejected(self):
        with connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO comment (article_slug, user_id, content, created_at, is_deleted) "
                    "VALUES ('a', 999999, 'x', 'now', 0)")
                conn.commit()

    def test_temporary_connections_are_closed(self):
        """各 db_* 模块必须关闭连接：删除数据库文件后不应残留句柄阻止删除。"""
        user_id, _ = self.create_user()
        get_user_by_id(user_id)
        get_user_number()
        get_user_by_email("alice@example.com")
        # Windows 上如果连接未关闭，删除会失败
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

    def test_no_connection_leak_after_repeated_calls(self):
        from elenvind.core.db_session import cleanup_expired_sessions
        self.create_user()
        for _ in range(50):
            get_user_number()
            cleanup_expired_sessions()
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())


class SchemaTests(ElenvindTestCase):
    def test_schema_version_is_recorded(self):
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_migrate_is_idempotent(self):
        for _ in range(3):
            migrate()
        with connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_init_db_is_idempotent(self):
        init_db()
        init_db()
        with connect() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("user", tables)
        self.assertIn("comment", tables)
        self.assertIn("session", tables)
        self.assertIn("login_attempts", tables)
        self.assertIn("comment_rate", tables)
        self.assertIn("register_attempts", tables)

    def test_comment_parent_foreign_key_uses_set_null(self):
        with connect() as conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'comment'").fetchone()[0]
        normalized = " ".join(sql.upper().split())
        self.assertIn("ON DELETE SET NULL", normalized)

    def test_legacy_schema_without_set_null_is_migrated(self):
        """旧库（parent_id 无 ON DELETE SET NULL）必须被就地迁移，且保留数据。"""
        legacy_dir = self.tmpdir / "legacy"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript("""
            CREATE TABLE user (
                id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
                nickname_changed_at TEXT);
            CREATE TABLE comment (
                id INTEGER PRIMARY KEY AUTOINCREMENT, article_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL, parent_id INTEGER,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES user(id));
            INSERT INTO user (nickname, email, password, created_at)
                VALUES ('Old', 'old@example.com', 'x', '2026-01-01T00:00:00');
            INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
                VALUES ('post', 1, 'root', '2026-01-01T00:00:01', NULL);
            INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
                VALUES ('post', 1, 'child', '2026-01-01T00:00:02', 1);
            INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
                VALUES ('post', 1, 'orphan', '2026-01-01T00:00:03', 4242);
        """)
        conn.commit()
        conn.close()

        # 优先级是 ELENVIND_DB > config.database > 默认路径，
        # 因此要让 init_db() 作用于这个旧库，必须连环境变量一起指向它。
        original_path = db_base.DB_PATH
        original_env = os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = legacy_path
        os.environ["ELENVIND_DB"] = str(legacy_path)
        try:
            init_db()
            self.assertEqual(Path(db_base.DB_PATH), legacy_path)   # 确认打在旧库上
            with connect() as check:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                rows = {row["content"]: row["parent_id"] for row in check.execute(
                    "SELECT content, parent_id FROM comment")}
                sql = check.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'comment'").fetchone()[0]
        finally:
            db_base.DB_PATH = original_path
            if original_env is None:
                os.environ.pop("ELENVIND_DB", None)
            else:
                os.environ["ELENVIND_DB"] = original_env

        self.assertEqual(rows["root"], None)
        self.assertEqual(rows["child"], 1)
        self.assertIsNone(rows["orphan"])          # 悬空父引用被修复
        self.assertIn("ON DELETE SET NULL", " ".join(sql.upper().split()))

    def test_legacy_session_table_with_expires_is_migrated(self):
        """旧 session 表（单一 expires 列）必须迁到 created_at + last_seen。

        关键不变式：**迁移不会延长任何已有会话的寿命**。
        旧实现写死 7 天，因此回填规则是

            created_at = expires - 7 天     （保留原来的绝对到期时刻）
            last_seen  = created_at

        于是老会话要么在原 expires 到期，要么在滑动窗口到期，取先到者。
        """
        import time as _time
        from elenvind.core.db_base import _LEGACY_SESSION_DAYS

        legacy_dir = self.tmpdir / "legacy-session"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "legacy.db"

        now = _time.time()
        # 一个尚未到期的老会话：expires 在 3 天后
        live_expires = now + 3 * 86400
        # 一个已经到期的老会话
        dead_expires = now - 3600
        conn = sqlite3.connect(legacy_path)
        conn.executescript(f"""
            CREATE TABLE user (
                id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
                nickname_changed_at TEXT);
            CREATE TABLE session (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE);
            INSERT INTO user (nickname, email, password, created_at)
                VALUES ('Old', 'old@example.com', 'x', '2026-01-01T00:00:00');
            INSERT INTO session (token, user_id, expires)
                VALUES ('live-token', 1, {live_expires});
            INSERT INTO session (token, user_id, expires)
                VALUES ('dead-token', 1, {dead_expires});
            PRAGMA user_version = 2;
        """)
        conn.commit()
        conn.close()

        original_path = db_base.DB_PATH
        original_env = os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = legacy_path
        os.environ["ELENVIND_DB"] = str(legacy_path)
        try:
            init_db()
            with connect() as check:
                version = check.execute("PRAGMA user_version").fetchone()[0]
                sql = check.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'session'"
                ).fetchone()[0]
                rows = {row["token"]: (row["created_at"], row["last_seen"])
                        for row in check.execute(
                            "SELECT token, created_at, last_seen FROM session")}
        finally:
            db_base.DB_PATH = original_path
            if original_env is None:
                os.environ.pop("ELENVIND_DB", None)
            else:
                os.environ["ELENVIND_DB"] = original_env

        self.assertEqual(version, SCHEMA_VERSION)
        normalized = " ".join(sql.upper().split())
        self.assertIn("CREATED_AT", normalized)
        self.assertIn("LAST_SEEN", normalized)
        self.assertNotIn("EXPIRES", normalized)

        legacy_span = _LEGACY_SESSION_DAYS * 86400
        self.assertAlmostEqual(rows["live-token"][0], live_expires - legacy_span,
                               delta=1)
        self.assertEqual(rows["live-token"][0], rows["live-token"][1],
                         "last_seen 应回填为 created_at（按'从未活跃'保守处理）")
        # 绝对到期时刻必须原封不动：created_at + 7 天 == 原 expires
        self.assertAlmostEqual(rows["live-token"][0] + legacy_span, live_expires,
                               delta=1)
        self.assertAlmostEqual(rows["dead-token"][0] + legacy_span, dead_expires,
                               delta=1)

    def test_migrated_legacy_session_keeps_original_deadline(self):
        """迁移后那个"还没到期"的老会话仍能通过校验，不会被凭空延长或提前踢掉。"""
        import time as _time
        from elenvind.core.db_base import _LEGACY_SESSION_DAYS
        from elenvind.core.db_session import get_session_user

        legacy_dir = self.tmpdir / "legacy-live"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "legacy.db"
        live_expires = _time.time() + 3 * 86400
        conn = sqlite3.connect(legacy_path)
        conn.executescript(f"""
            CREATE TABLE user (
                id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
                nickname_changed_at TEXT);
            CREATE TABLE session (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                expires REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE);
            INSERT INTO user (nickname, email, password, created_at)
                VALUES ('Old', 'old@example.com', 'x', '2026-01-01T00:00:00');
            INSERT INTO session (token, user_id, expires)
                VALUES ('live-token', 1, {live_expires});
            PRAGMA user_version = 2;
        """)
        conn.commit()
        conn.close()

        original_path = db_base.DB_PATH
        original_env = os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = legacy_path
        os.environ["ELENVIND_DB"] = str(legacy_path)
        try:
            init_db()
            # 默认绝对 30 天 / 滑动 15 天，7 天前创建的会话两个维度都没超
            self.assertEqual(get_session_user("live-token"), 1)
        finally:
            db_base.DB_PATH = original_path
            if original_env is None:
                os.environ.pop("ELENVIND_DB", None)
            else:
                os.environ["ELENVIND_DB"] = original_env

    def test_parent_deletion_sets_child_parent_to_null(self):
        user_id, _ = self.create_user()
        parent = create_comment("post", user_id, "root")
        child = create_comment("post", user_id, "child", parent_id=parent)
        with connect() as conn:
            conn.execute("DELETE FROM comment WHERE id = ?", (parent,))
            conn.commit()
        self.assertIsNone(get_comment_by_id(child)["parent_id"])

    def test_user_deletion_cascades_to_sessions(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        with connect() as conn:
            conn.execute("DELETE FROM user WHERE id = ?", (user_id,))
            conn.commit()
        self.assertIsNone(get_session_user(token))


class UserTableTests(ElenvindTestCase):
    def test_email_unique_constraint_is_authoritative(self):
        create_user("A", "dup@example.com", hash_password("password-123"))
        with self.assertRaises(sqlite3.IntegrityError):
            create_user("B", "dup@example.com", hash_password("password-123"))

    def test_email_lookup_is_case_insensitive(self):
        create_user("A", "Case@Example.COM", hash_password("password-123"))
        self.assertIsNotNone(get_user_by_email("case@example.com"))

    def test_logical_delete_hides_user_and_frees_email(self):
        user_id = create_user("A", "gone@example.com", hash_password("password-123"))
        delete_user(user_id)
        self.assertIsNone(get_user_by_id(user_id))
        self.assertIsNone(get_user_by_email("gone@example.com"))
        # 占位邮箱不与未来注册冲突
        create_user("A2", "gone@example.com", hash_password("password-123"))

    def test_delete_unknown_user_raises(self):
        with self.assertRaises(ValueError):
            delete_user(12345)

    def test_user_number_counts_only_active(self):
        first = create_user("A", "a@example.com", hash_password("password-123"))
        create_user("B", "b@example.com", hash_password("password-123"))
        self.assertEqual(get_user_number(), 2)
        delete_user(first)
        self.assertEqual(get_user_number(), 1)

    def test_user_number_cache_invalidated_on_delete(self):
        create_user("A", "a@example.com", hash_password("password-123"))
        self.assertEqual(get_user_number(), 1)
        second = create_user("B", "b@example.com", hash_password("password-123"))
        self.assertEqual(get_user_number(), 2)
        delete_user(second)
        self.assertEqual(get_user_number(), 1)

    def test_updates_persist(self):
        user_id = create_user("A", "a@example.com", hash_password("password-123"))
        update_user_profile(user_id, nickname="Renamed", email="new@example.com")
        update_user_password(user_id, "scrypt$ln=15,r=8,p=1$00$11")
        row = get_user_by_id(user_id)
        self.assertEqual(row["nickname"], "Renamed")
        self.assertEqual(row["email"], "new@example.com")
        self.assertEqual(row["password"], "scrypt$ln=15,r=8,p=1$00$11")
        self.assertIsNotNone(row["nickname_changed_at"])

    def test_email_update_collision_raises_integrity_error(self):
        first = create_user("A", "a@example.com", hash_password("password-123"))
        create_user("B", "b@example.com", hash_password("password-123"))
        with self.assertRaises(sqlite3.IntegrityError):
            update_user_profile(first, email="b@example.com")


class SessionTableTests(ElenvindTestCase):
    def test_session_lifecycle(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        self.assertEqual(get_session_user(token), user_id)
        delete_user_sessions(user_id)
        self.assertIsNone(get_session_user(token))

    def test_session_tokens_are_unique(self):
        user_id, _ = self.create_user()
        tokens = {create_session(user_id) for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    def test_unknown_session_returns_none(self):
        self.assertIsNone(get_session_user("nope"))
        self.assertIsNone(get_session_user(""))

    def test_session_foreign_key_requires_existing_user(self):
        with self.assertRaises(sqlite3.IntegrityError):
            create_session(999999)


class RateLimitTableTests(ElenvindTestCase):
    def test_login_attempt_counters(self):
        record_login_attempt("a@example.com", "1.2.3.4", success=False)
        record_login_attempt("a@example.com", "1.2.3.4", success=True)
        self.assertEqual(count_email_failures("a@example.com", 900), 1)
        self.assertEqual(count_ip_failures("1.2.3.4", 900), 1)
        self.assertEqual(count_email_failures("A@Example.com", 900), 1)   # 大小写不敏感

    def test_cleanup_removes_old_rows(self):
        import time

        record_login_attempt("a@example.com", "1.2.3.4", success=False)
        with connect() as conn:
            conn.execute("UPDATE login_attempts SET attempted_at = 0")
            conn.commit()
        huge_window = int(time.time()) + 10 ** 6        # 覆盖 1970 年以来的全部记录
        self.assertEqual(count_email_failures("a@example.com", huge_window), 1)
        cleanup_old_login_attempts(days=1)
        self.assertEqual(count_email_failures("a@example.com", huge_window), 0)

    def test_register_attempt_limiter_is_atomic(self):
        self.assertTrue(try_register_attempt("9.9.9.9", max_per_ip=2, window_seconds=60))
        self.assertTrue(try_register_attempt("9.9.9.9", max_per_ip=2, window_seconds=60))
        self.assertFalse(try_register_attempt("9.9.9.9", max_per_ip=2, window_seconds=60))
        # 表里确实落了 2 行（第三个被拒，没有记账）。
        # 这里直接读表，而不是调用已删除的 `count_recent()` ——
        # 那个函数没有任何生产调用方（限流判定在事务内自己 COUNT），
        # 只是为了让这条断言好写而存在，属于死代码。
        with connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM register_attempts WHERE ip = ?",
                ("9.9.9.9",)).fetchone()[0]
        self.assertEqual(rows, 2)

    def test_register_attempt_is_per_ip(self):
        self.assertTrue(try_register_attempt("1.1.1.1", max_per_ip=1, window_seconds=60))
        self.assertTrue(try_register_attempt("2.2.2.2", max_per_ip=1, window_seconds=60))


class SqlInjectionTests(ElenvindTestCase):
    """所有查询都是参数化的：注入字符串只会被当作普通数据。"""

    PAYLOADS = (
        "a@example.com' OR '1'='1",
        "'; DROP TABLE user; --",
        "x'; UPDATE user SET is_deleted = 1; --",
        '" OR 1=1 --',
    )

    def test_email_lookup_is_parameterized(self):
        create_user("A", "safe@example.com", hash_password("password-123"))
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                result = get_user_by_email(payload)
                self.assertIsNone(result)
                self.assertIsNotNone(get_user_by_email("safe@example.com"))

    def test_comment_slug_lookup_is_parameterized(self):
        user_id, _ = self.create_user()
        create_comment("post", user_id, "content")
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                self.assertEqual(list(get_comments_by_article(payload)), [])
                self.assertEqual(len(get_comments_by_article("post")), 1)

    def test_comment_content_roundtrips_verbatim(self):
        user_id, _ = self.create_user()
        payload = "'); DROP TABLE comment; --"
        comment_id = create_comment("post", user_id, payload)
        self.assertEqual(get_comment_by_id(comment_id)["content"], payload)
        self.assertEqual(len(get_comments_by_article("post")), 1)

    #: 允许的动态 SQL 站点（键=相对 elenvind/ 的路径，值=该文件允许出现的次数）。
    #: 只放行"插值内容不是用户输入"的情形，每一处都必须在这里说明理由：
    #:   core/db_base.py（3 处）
    #:     - `PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}`：模块常量；
    #:     - `PRAGMA user_version={int(version)}`：int() 强转后的整数；
    #:     - `SELECT COUNT(*) FROM {table}`：表名是代码里写死的字面量。
    #:     （SQLite 的 PRAGMA 不支持参数绑定，这两处 f-string 无法避免。）
    #:   core/db_session.py（1 处）
    #:     - `DELETE FROM session WHERE {' OR '.join(clauses)}`：
    #:       拼接的是本函数内构造的常量子句，阈值全部走 params 绑定。
    #:   core/db_user.py（1 处）
    #:     - `UPDATE user SET {', '.join(fields)}`（update_user_profile）：
    #:       拼接的是字面量列名（"nickname = ?" / "email = ?"），
    #:       所有**值**都走 params 绑定；列名不来自任何外部输入。
    #:   core/db_prune.py（1 处）
    #:     - `DELETE FROM {table} WHERE {column} < ?`（prune）：
    #:       表名/列名由调用方以字面量传入（如 "comment_rate"/"attempted_at"），
    #:       阈值走 params 绑定；本模块不接受任何外部输入。
    ALLOWED_DYNAMIC_SQL = {
        "core/db_base.py": 3,
        "core/db_session.py": 1,
        "core/db_user.py": 1,
        "core/db_prune.py": 1,
    }

    def test_no_sql_string_interpolation_in_source(self):
        """静态检查：SQL 不得用 f-string 拼入**用户可控**内容。

        回归：本测试曾经双重失效 ——
        1. `glob("elenvind/db_*.py")` 命中 0 个文件（db_*.py 在 core/ 下），
           循环体从不执行，`assertEqual([], [])` 永远通过；
        2. 正则要求「闭合引号后紧跟另一个引号」，对真正的
           `f"SELECT ... '{email}'"` 也匹配不到。
        于是把任何查询改成 f-string 注入都不会被发现。
        """
        import ast
        from tests.support import PROJECT_ROOT

        root = PROJECT_ROOT / "elenvind"
        found = {}
        scanned = 0
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", "") not in ("execute", "executemany",
                                                          "executescript"):
                    continue
                if not node.args:
                    continue
                arg = node.args[0]
                text = ast.get_source_segment(source, arg) or ""
                if not any(marker in text.upper() for marker in
                           ("SELECT", "INSERT", "UPDATE", "DELETE", "PRAGMA",
                            "CREATE", "ALTER", "DROP")):
                    continue
                dynamic = isinstance(arg, ast.JoinedStr)          # f"..."
                if isinstance(arg, ast.BinOp):                     # "..." + x
                    dynamic = True
                if (isinstance(arg, ast.Call)
                        and getattr(arg.func, "attr", "") == "format"):
                    dynamic = True
                if dynamic:
                    key = str(path.relative_to(root)).replace("\\", "/")
                    found[key] = found.get(key, 0) + 1

        # 守卫自身必须真的扫到了东西（否则又是一次"命中 0 个文件"）
        self.assertGreater(scanned, 20, f"只扫描了 {scanned} 个文件，路径不对")
        self.assertGreater(sum(found.values()), 0,
                           "一个动态 SQL 站点都没找到，守卫已失效")

        unexpected = {key: count for key, count in found.items()
                      if self.ALLOWED_DYNAMIC_SQL.get(key, 0) != count}
        self.assertEqual(
            unexpected, {},
            "动态 SQL 站点发生变化。新增拼接前请确认插值内容不是用户输入"
            "（SQLite 的 PRAGMA 不支持参数绑定，是唯一合理的例外）：\n"
            f"  实际: {found}\n  允许: {self.ALLOWED_DYNAMIC_SQL}")


class MigrationTransactionTests(ElenvindTestCase):
    """迁移必须整体成功或整体回滚，半迁移态必须被拦下。

    回归背景：`_run_migrations` 原先没有显式事务，而 CPython 的 sqlite3
    **只为 DML 开隐式事务，DDL 不开**。表重建里的 `ALTER TABLE ... RENAME`
    与 `CREATE TABLE` 都是 DDL，会被立即提交；一旦后续步骤失败或进程被杀，
    就留下"备份表有数据、目标表为空"的状态。更糟的是迁移靠子串匹配判断
    "已迁移"，`_create_schema` 又会建出空的同形表，于是 user_version 被写成
    最新版而评论数据永久搁置在 `*_legacy` 表里 —— 站点看着正常，评论全没了。
    """

    LEGACY_SCHEMA = """
        CREATE TABLE user (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
            created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
            nickname_changed_at TEXT);
        CREATE TABLE comment (
            id INTEGER PRIMARY KEY AUTOINCREMENT, article_slug TEXT NOT NULL,
            user_id INTEGER NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL, parent_id INTEGER,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES user(id));
        CREATE TABLE session (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE);
        INSERT INTO user (nickname, email, password, created_at)
            VALUES ('Old','old@example.com','x','2026-01-01T00:00:00');
        INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
            VALUES ('post',1,'c1','2026-01-01T00:00:01',NULL);
        INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
            VALUES ('post',1,'c2','2026-01-01T00:00:02',1);
        INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
            VALUES ('post',1,'c3','2026-01-01T00:00:03',2);
        PRAGMA user_version = 1;
    """

    def _legacy_db(self, name):
        path = self.tmpdir / name
        conn = sqlite3.connect(path)
        conn.executescript(self.LEGACY_SCHEMA)
        conn.commit()
        conn.close()
        return path

    def _with_db(self, path, fn):
        original_path, original_env = db_base.DB_PATH, os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = path
        os.environ["ELENVIND_DB"] = str(path)
        try:
            return fn()
        finally:
            db_base.DB_PATH = original_path
            if original_env is None:
                os.environ.pop("ELENVIND_DB", None)
            else:
                os.environ["ELENVIND_DB"] = original_env

    def _state(self, path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = sorted(row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        count = conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0]
        comment_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comment'").fetchone()[0] or ""
        conn.close()
        return version, tables, count, comment_sql

    def test_failed_migration_rolls_back_ddl_too(self):
        """迁移中途失败时，连 ALTER/CREATE 这类 DDL 也必须回滚。"""
        from elenvind.core import db_base as module
        from elenvind.core.db_base import migrate

        path = self._legacy_db("rollback.db")
        before_version, _, before_count, before_sql = self._state(path)

        original = module._MIGRATIONS[2]

        def exploding(conn):
            original(conn)                     # 真正执行重建（ALTER + CREATE + INSERT）
            raise RuntimeError("simulated mid-migration failure")

        module._MIGRATIONS[2] = exploding
        try:
            with self.assertRaises(RuntimeError):
                self._with_db(path, migrate)
        finally:
            module._MIGRATIONS[2] = original

        version, tables, count, comment_sql = self._state(path)
        self.assertEqual(version, before_version, "user_version 不该被推进")
        self.assertEqual(count, before_count, "评论行数不该变化")
        self.assertEqual([t for t in tables if t.endswith("_legacy")], [],
                         "不该留下备份表")
        self.assertNotIn("ON DELETE SET NULL", comment_sql.upper(),
                         "DDL 没有回滚：comment 表已被改成新形态")

    def test_leftover_backup_table_refuses_to_start(self):
        """发现 *_legacy 残留必须拒绝启动，而不是继续并搁置数据。"""
        from elenvind.core.db_base import MigrationError, SCHEMA_VERSION, init_db

        path = self._legacy_db("leftover.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            ALTER TABLE comment RENAME TO comment_legacy;
            CREATE TABLE comment (
                id INTEGER PRIMARY KEY AUTOINCREMENT, article_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL, parent_id INTEGER,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES user(id),
                FOREIGN KEY(parent_id) REFERENCES comment(id) ON DELETE SET NULL);
        """)
        conn.commit()
        conn.close()

        with self.assertRaises(MigrationError):
            self._with_db(path, init_db)

        version, tables, count, _ = self._state(path)
        self.assertNotEqual(version, SCHEMA_VERSION, "不该把半迁移态标记为最新")
        self.assertIn("comment_legacy", tables)
        # 数据必须还在备份表里（没有被 DROP 掉）
        conn = sqlite3.connect(path)
        legacy_count = conn.execute(
            "SELECT COUNT(*) FROM comment_legacy").fetchone()[0]
        conn.close()
        self.assertEqual(legacy_count, 3, "备份表里的评论被弄丢了")
        self.assertEqual(count, 0)

    def test_normal_migration_preserves_data_and_is_idempotent(self):
        from elenvind.core.db_base import SCHEMA_VERSION, init_db, migrate

        path = self._legacy_db("normal.db")
        self._with_db(path, init_db)
        version, tables, count, _ = self._state(path)
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(count, 3)
        self.assertEqual([t for t in tables if t.endswith("_legacy")], [])

        for _ in range(3):
            self._with_db(path, migrate)
        version, _, count, _ = self._state(path)
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(count, 3)

    def test_begin_immediate_is_issued_before_ddl(self):
        """结构性守卫：迁移必须在执行迁移函数**之前**开启事务。"""
        import inspect
        from elenvind.core import db_base as module

        source = inspect.getsource(module._run_migrations)
        begin = source.index("BEGIN IMMEDIATE")
        call = source.index("migration(conn)")
        self.assertLess(begin, call,
                        "BEGIN IMMEDIATE 必须出现在 migration() 调用之前，"
                        "否则 DDL 仍会被立即提交")


class HotQueryIndexTests(ElenvindTestCase):
    """热查询必须走索引，不能全表扫描。

    回归：两处真实的性能缺陷 ——
    1. `user.email` 的隐式 UNIQUE 索引是 **BINARY** 排序规则，而查询写成
       `email = ? COLLATE NOCASE`，排序规则不匹配 -> 索引用不上 ->
       **每次登录/注册/改邮箱都全表扫描 user 表**；
    2. `session.user_id` 干脆没有索引，而 `delete_user_sessions()`
       每次登录轮换都会跑。

    这类退化不会让测试变红，只会在数据量上来后悄悄变慢，
    所以用 EXPLAIN QUERY PLAN 直接锁住"必须 SEARCH 不能 SCAN"。
    """

    #: (说明, SQL, 参数个数)
    HOT_QUERIES = (
        ("按邮箱查用户（每次登录）",
         "SELECT * FROM user WHERE email = ? AND is_deleted = 0", 1),
        ("按 id 查用户（每次请求鉴权）",
         "SELECT * FROM user WHERE id = ? AND is_deleted = 0", 1),
        ("删除某用户全部会话（每次登录轮换）",
         "DELETE FROM session WHERE user_id = ?", 1),
        ("按 token 查会话",
         "SELECT user_id FROM session WHERE token = ?", 1),
        ("按文章取评论",
         "SELECT * FROM comment WHERE article_slug = ?", 1),
        ("评论限流按用户",
         "SELECT COUNT(*) FROM comment_rate WHERE user_id = ? AND attempted_at > ?", 2),
        ("登录流水按邮箱",
         "SELECT COUNT(*) FROM login_attempts WHERE email = ? AND attempted_at > ?", 2),
    )

    def _plan(self, sql, count):
        with connect() as conn:
            rows = conn.execute("EXPLAIN QUERY PLAN " + sql,
                                tuple("x" for _ in range(count))).fetchall()
        return "; ".join(row[3] for row in rows)

    @staticmethod
    def _is_full_scan(detail: str) -> bool:
        """是否是**没有索引**的全表扫描。

        SQLite 对"只需要索引里的列"的查询会输出
        `SCAN <table> USING COVERING INDEX <idx>` —— 那是扫一个窄索引，
        比走表快得多，属于正常优化，不能当成缺陷。
        真正的缺陷是只写 `SCAN <table>`（没有任何 USING）。
        """
        upper = detail.upper()
        if "SEARCH" in upper:
            return False
        for part in upper.split(";"):
            part = part.strip()
            if part.startswith("SCAN") and "USING" not in part:
                return True
        return False

    def test_hot_queries_use_index_not_scan(self):
        for label, sql, count in self.HOT_QUERIES:
            with self.subTest(query=label):
                detail = self._plan(sql, count)
                self.assertFalse(self._is_full_scan(detail),
                                 f"{label} 出现无索引全表扫描：{detail}")
                upper = detail.upper()
                self.assertTrue("SEARCH" in upper or "USING" in upper,
                                f"{label} 既没走索引也没用覆盖索引：{detail}")

    def test_session_has_user_id_index(self):
        with connect() as conn:
            names = [row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='session'")]
        self.assertIn("idx_session_user", names)

    def test_email_lookup_sql_does_not_collate(self):
        """源码守卫：`get_user_by_email` 的 **SQL** 里不得出现 COLLATE。

        只看字符串字面量：docstring 里必然提到 `COLLATE NOCASE`（那是在解释
        为什么不能用它），直接扫整个函数源码会被自己的注释骗到。
        """
        import ast
        import inspect
        import textwrap
        from elenvind.core.db_user import get_user_by_email

        tree = ast.parse(textwrap.dedent(inspect.getsource(get_user_by_email)))
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        for literal in literals:
            if "SELECT" in literal.upper() or "DELETE" in literal.upper():
                self.assertNotIn("COLLATE", literal.upper(),
                                 f"SQL 字面量里出现 COLLATE，会让查询退回全表扫描：{literal}")

    def test_writes_normalize_email(self):
        """写入侧必须规范化大小写（大小写不敏感靠数据规范化，而不是查询比较）。"""
        mixed = f"MiXeD-{os.urandom(4).hex()}@Example.COM"
        user_id = create_user("A", mixed, hash_password("password-123"))
        with connect() as conn:
            stored = conn.execute("SELECT email FROM user WHERE id = ?",
                                  (user_id,)).fetchone()["email"]
        self.assertEqual(stored, stored.lower())
        self.assertEqual(stored, mixed.lower())
        # 任意大小写都能查到
        self.assertIsNotNone(get_user_by_email(mixed.upper()))
        self.assertIsNotNone(get_user_by_email(mixed.lower()))


class SchemaV4MigrationTests(ElenvindTestCase):
    """v4 迁移：补会话索引 + 规范化历史大写邮箱。"""

    def test_legacy_uppercase_emails_are_normalized(self):
        from elenvind.core.db_base import SCHEMA_VERSION, init_db

        path = self.tmpdir / "v3.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE user (
                id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
                nickname_changed_at TEXT);
            CREATE TABLE session (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                created_at REAL NOT NULL, last_seen REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE);
            CREATE TABLE comment (
                id INTEGER PRIMARY KEY AUTOINCREMENT, article_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL, parent_id INTEGER,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES user(id),
                FOREIGN KEY(parent_id) REFERENCES comment(id) ON DELETE SET NULL);
            INSERT INTO user (nickname, email, password, created_at)
                VALUES ('A','Alice@Example.COM','h','2026-01-01T00:00:00');
            INSERT INTO user (nickname, email, password, created_at)
                VALUES ('B','bob@example.com','h','2026-01-01T00:00:00');
            INSERT INTO comment (article_slug, user_id, content, created_at)
                VALUES ('post',1,'c','2026-01-01T00:00:01');
            INSERT INTO session (token, user_id, created_at, last_seen)
                VALUES ('t',1,1.0,1.0);
            PRAGMA user_version = 3;
        """)
        conn.commit()
        conn.close()

        original_path, original_env = db_base.DB_PATH, os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = path
        os.environ["ELENVIND_DB"] = str(path)
        try:
            init_db()
            with connect() as check:
                version = check.execute("PRAGMA user_version").fetchone()[0]
                emails = [row["email"] for row in
                          check.execute("SELECT email FROM user ORDER BY id")]
                counts = (check.execute("SELECT COUNT(*) FROM user").fetchone()[0],
                          check.execute("SELECT COUNT(*) FROM comment").fetchone()[0],
                          check.execute("SELECT COUNT(*) FROM session").fetchone()[0])
                indexes = [row["name"] for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='session'")]
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(emails, ["alice@example.com", "bob@example.com"])
            self.assertEqual(counts, (2, 1, 1), "迁移不得丢数据")
            self.assertIn("idx_session_user", indexes)
        finally:
            db_base.DB_PATH = original_path
            if original_env is None:
                os.environ.pop("ELENVIND_DB", None)
            else:
                os.environ["ELENVIND_DB"] = original_env

    def test_migration_is_idempotent(self):
        from elenvind.core.db_base import migrate
        before = self._snapshot()
        for _ in range(3):
            migrate()
        self.assertEqual(self._snapshot(), before)

    def _snapshot(self):
        with connect() as conn:
            return (conn.execute("PRAGMA user_version").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM user").fetchone()[0])


class AtomicProfileUpdateTests(ElenvindTestCase):
    """改资料必须原子：昵称与邮箱要么都改，要么都不改。

    回归：旧实现是两次独立的 `UPDATE`（各自开连接、各自提交）。
    并发场景下邮箱可能被别人抢先占用 -> 邮箱那条抛 IntegrityError 回滚，
    而昵称那条**已经提交** —— 用户看到"邮箱已被占用"，昵称却已经悄悄改了，
    页面上显示的却还是旧行（半写状态）。
    """

    def test_both_fields_are_updated_together(self):
        user_id = create_user("Old", "old@example.com", hash_password("password-123"))
        changed = update_user_profile(user_id, nickname="New", email="new@example.com")
        self.assertTrue(changed)
        with connect() as conn:
            row = conn.execute(
                "SELECT nickname, email FROM user WHERE id = ?", (user_id,)).fetchone()
        self.assertEqual((row["nickname"], row["email"]),
                         ("New", "new@example.com"))

    def test_nickname_only(self):
        user_id = create_user("Old", "keep@example.com", hash_password("password-123"))
        update_user_profile(user_id, nickname="New")
        with connect() as conn:
            row = conn.execute(
                "SELECT nickname, email, nickname_changed_at FROM user WHERE id = ?",
                (user_id,)).fetchone()
        self.assertEqual(row["nickname"], "New")
        self.assertEqual(row["email"], "keep@example.com")
        self.assertIsNotNone(row["nickname_changed_at"])

    def test_email_only(self):
        user_id = create_user("Keep", "a@example.com", hash_password("password-123"))
        update_user_profile(user_id, email="b@example.com")
        with connect() as conn:
            row = conn.execute(
                "SELECT nickname, email, nickname_changed_at FROM user WHERE id = ?",
                (user_id,)).fetchone()
        self.assertEqual(row["nickname"], "Keep")
        self.assertEqual(row["email"], "b@example.com")
        self.assertIsNone(row["nickname_changed_at"], "只改邮箱不该动昵称时间")

    def test_conflicting_email_rolls_back_the_nickname_too(self):
        """核心回归：邮箱冲突时昵称也不得改动。"""
        first = create_user("First", "taken@example.com", hash_password("password-123"))
        second = create_user("Second", "second@example.com",
                             hash_password("password-123"))
        with self.assertRaises(sqlite3.IntegrityError):
            update_user_profile(second, nickname="Should Not Stick",
                                email="taken@example.com")
        with connect() as conn:
            row = conn.execute(
                "SELECT nickname, email FROM user WHERE id = ?",
                (second,)).fetchone()
        self.assertEqual(row["nickname"], "Second",
                         "邮箱冲突时昵称被写入了 —— 半写状态")
        self.assertEqual(row["email"], "second@example.com")

    def test_email_is_normalized_on_update(self):
        user_id = create_user("A", "a@example.com", hash_password("password-123"))
        update_user_profile(user_id, email="MiXeD@Example.COM")
        with connect() as conn:
            stored = conn.execute("SELECT email FROM user WHERE id = ?",
                                  (user_id,)).fetchone()["email"]
        self.assertEqual(stored, "mixed@example.com")

    def test_no_fields_means_no_write(self):
        user_id = create_user("A", "a@example.com", hash_password("password-123"))
        self.assertFalse(update_user_profile(user_id))

    def test_soft_deleted_user_is_not_updated(self):
        user_id = create_user("A", "a@example.com", hash_password("password-123"))
        delete_user(user_id)
        self.assertFalse(update_user_profile(user_id, nickname="Nope"),
                         "已注销用户不该被更新")
        with connect() as conn:
            nickname = conn.execute("SELECT nickname FROM user WHERE id = ?",
                                    (user_id,)).fetchone()["nickname"]
        self.assertEqual(nickname, "Ghost")


class BodyCacheBoundTests(ElenvindTestCase):
    """正文缓存必须有界（重命名/重写文章会留下永不命中的死键）。"""

    def test_cache_does_not_grow_without_bound(self):
        from elenvind.features.blog import logic as blog_logic
        from elenvind.core.config import config as live_config

        limit = blog_logic.BODY_CACHE_MAX_ENTRIES
        directory = self.tmpdir / "many"
        directory.mkdir(exist_ok=True)
        for index in range(limit + 40):
            (directory / f"a{index}.md").write_text(
                f'+++\ntitle = "A{index}"\ndate = "2026-01-01"\n+++\n\nbody {index}\n',
                encoding="utf-8")

        original = live_config.get("articles_dir")
        live_config["articles_dir"] = str(directory)
        blog_logic._articles_cache = None
        blog_logic._body_cache.clear()
        try:
            for index in range(limit + 40):
                blog_logic.load_article_body(f"a{index}")
            self.assertLessEqual(len(blog_logic._body_cache), limit,
                                 "正文缓存超过了上限")
        finally:
            if original is None:
                live_config.pop("articles_dir", None)
            else:
                live_config["articles_dir"] = original
            blog_logic._articles_cache = None
            blog_logic._body_cache.clear()

    def test_rescan_evicts_dead_keys(self):
        """文章被删/改名后，重扫应当把它的缓存条目清掉。"""
        from elenvind.features.blog import logic as blog_logic

        directory = self.tmpdir / "dead"
        directory.mkdir(exist_ok=True)
        (directory / "keep.md").write_text(
            '+++\ntitle = "Keep"\ndate = "2026-01-01"\n+++\n\nbody\n', encoding="utf-8")
        (directory / "gone.md").write_text(
            '+++\ntitle = "Gone"\ndate = "2026-01-02"\n+++\n\nbody\n', encoding="utf-8")

        from elenvind.core.config import config as live_config
        original = live_config.get("articles_dir")
        live_config["articles_dir"] = str(directory)
        blog_logic._articles_cache = None
        blog_logic._body_cache.clear()
        try:
            blog_logic.load_article_body("keep")
            blog_logic.load_article_body("gone")
            self.assertEqual(len(blog_logic._body_cache), 2)

            (directory / "gone.md").unlink()
            blog_logic._articles_cache = None          # 强制重扫
            blog_logic.get_articles()                  # 触发 _rescan -> 淘汰死键
            keys = [Path(key).name for key in blog_logic._body_cache]
            self.assertIn("keep.md", keys)
            self.assertNotIn("gone.md", keys, "死键没有被淘汰")
        finally:
            if original is None:
                live_config.pop("articles_dir", None)
            else:
                live_config["articles_dir"] = original
            blog_logic._articles_cache = None
            blog_logic._body_cache.clear()


if __name__ == "__main__":
    unittest.main()
