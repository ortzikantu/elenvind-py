"""数据库层测试：外键、schema 迁移、连接生命周期、约束、限流流水与数据完整性。"""
import sqlite3
import unittest

from tests.support import ElenvindTestCase

from elenvind import db_base
from elenvind.db_base import SCHEMA_VERSION, get_connection, init_db, migrate
from elenvind.db_comment import create_comment, get_comment_by_id, get_comments_by_article
from elenvind.db_login import (
    cleanup_old_login_attempts,
    count_email_failures,
    count_ip_failures,
    record_login_attempt,
)
from elenvind.db_register import count_recent, try_register_attempt
from elenvind.db_session import create_session, delete_user_sessions, get_session_user
from elenvind.db_user import (
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_id,
    get_user_number,
    update_user_email,
    update_user_nickname,
    update_user_password,
)
from elenvind.security import hash_password


class ConnectionTests(ElenvindTestCase):
    def test_foreign_keys_enabled_on_every_connection(self):
        for _ in range(3):
            with get_connection() as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_wal_mode_and_busy_timeout(self):
        with get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertGreaterEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_row_factory_is_row(self):
        with get_connection() as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)

    def test_foreign_key_violation_is_rejected(self):
        with get_connection() as conn:
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
        from elenvind.db_session import cleanup_expired_sessions
        self.create_user()
        for _ in range(50):
            get_user_number()
            cleanup_expired_sessions()
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())


class SchemaTests(ElenvindTestCase):
    def test_schema_version_is_recorded(self):
        with get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_migrate_is_idempotent(self):
        for _ in range(3):
            migrate()
        with get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_init_db_is_idempotent(self):
        init_db()
        init_db()
        with get_connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("user", tables)
        self.assertIn("comment", tables)
        self.assertIn("session", tables)
        self.assertIn("login_attempts", tables)
        self.assertIn("comment_rate", tables)
        self.assertIn("register_attempts", tables)

    def test_comment_parent_foreign_key_uses_set_null(self):
        with get_connection() as conn:
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

        original = db_base.DB_PATH
        db_base.DB_PATH = legacy_path
        try:
            init_db()
            with get_connection() as check:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                rows = {row["content"]: row["parent_id"] for row in check.execute(
                    "SELECT content, parent_id FROM comment")}
                sql = check.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'comment'").fetchone()[0]
        finally:
            db_base.DB_PATH = original

        self.assertEqual(rows["root"], None)
        self.assertEqual(rows["child"], 1)
        self.assertIsNone(rows["orphan"])          # 悬空父引用被修复
        self.assertIn("ON DELETE SET NULL", " ".join(sql.upper().split()))

    def test_parent_deletion_sets_child_parent_to_null(self):
        user_id, _ = self.create_user()
        parent = create_comment("post", user_id, "root")
        child = create_comment("post", user_id, "child", parent_id=parent)
        with get_connection() as conn:
            conn.execute("DELETE FROM comment WHERE id = ?", (parent,))
            conn.commit()
        self.assertIsNone(get_comment_by_id(child)["parent_id"])

    def test_user_deletion_cascades_to_sessions(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        with get_connection() as conn:
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
        update_user_nickname(user_id, "Renamed")
        update_user_email(user_id, "new@example.com")
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
            update_user_email(first, "b@example.com")


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
        with get_connection() as conn:
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
        self.assertEqual(count_recent("9.9.9.9", 60), 2)

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

    def test_no_sql_string_interpolation_in_source(self):
        """静态检查：SQL 语句不得用 f-string / .format 拼接。"""
        import re
        from tests.support import PROJECT_ROOT

        offenders = []
        for path in sorted((PROJECT_ROOT / "elenvind").glob("db_*.py")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r'(SELECT|INSERT|UPDATE|DELETE|CREATE)[^"\'\n]*"\s*[fF]?["\']', source):
                if "f\"" in match.group(0) or "f'" in match.group(0):
                    offenders.append((path.name, match.group(0)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
