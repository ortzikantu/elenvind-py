"""登录限流的安全回归测试（Phase 4-A：原子预留 + 渐进 backoff）。

覆盖三件此前**没有测试**的事：

1. 并发不超发：判定与记账在同一个写事务里，N 个并发尝试只能放行 `max` 个
   （旧实现是"读计数 → scrypt → 记账"，并发会同时读到低计数而全部放行）；
2. 渐进 backoff 而不是硬锁：达到阈值后等待时间从 base 开始按次数翻倍，
   冷却自然结束后**正确密码可以登录**（不再是一刀切 24 小时）；
3. 可观测：被限流时下发 `Retry-After`，文案带上真实等待时长。
"""
import sqlite3
import threading
import time
import unittest

from tests.support import ElenvindTestCase

from elenvind import db
from elenvind.db import connection as db_connection
from elenvind.db.auth import COOLDOWN_BASE_SECONDS, reserve_login_attempt


class ReserveAtomicityTests(ElenvindTestCase):
    """`reserve_login_attempt()` 的并发语义（纯 db 层，真线程）。"""

    def _reserve(self, results, index, email, ip):
        try:
            allowed, reason, retry_after = reserve_login_attempt(
                email, ip,
                max_email_failures=3, email_window_seconds=3600,
                max_ip_failures=100, ip_window_seconds=3600,
                max_global_failures=1000, global_window_seconds=3600,
            )
            results[index] = (allowed, reason, retry_after)
        except Exception as error:                     # noqa: BLE001
            results[index] = ("error", f"{type(error).__name__}: {error}", 0)

    def test_concurrent_attempts_cannot_exceed_the_threshold(self):
        """8 个并发尝试、阈值 3：恰好 3 个被放行。"""
        results = [None] * 8
        threads = [threading.Thread(target=self._reserve,
                                    args=(results, index, "race@example.com", "10.0.0.1"))
                   for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertNotIn(None, results, "有线程没有返回结果")
        allowed = [item for item in results if item[0] is True]
        blocked = [item for item in results if item[0] is False]
        self.assertEqual(len(allowed), 3,
                         f"阈值 3 却放行了 {len(allowed)} 次：{results}")
        self.assertEqual(len(blocked), 5)
        for _, reason, retry_after in blocked:
            self.assertEqual(reason, "email")
            self.assertGreaterEqual(retry_after, 1)

    def test_placeholder_row_is_written_before_verification(self):
        """放行时会先写一行占位（= 这次尝试），失败无需再记账。"""
        allowed, _, _ = reserve_login_attempt(
            "ph@example.com", "10.0.0.2",
            max_email_failures=5, email_window_seconds=3600,
            max_ip_failures=100, ip_window_seconds=3600,
            max_global_failures=1000, global_window_seconds=3600)
        self.assertTrue(allowed)
        self.assertEqual(db.count_email_failures("ph@example.com", 3600), 1)

    def test_blocked_attempt_writes_nothing(self):
        """被拦下的尝试不写行：否则攻击者能靠"被拒"继续推高计数（自我放大）。"""
        for _ in range(2):
            reserve_login_attempt("no-write@example.com", "10.0.0.3",
                                  max_email_failures=2, email_window_seconds=3600,
                                  max_ip_failures=100, ip_window_seconds=3600,
                                  max_global_failures=1000, global_window_seconds=3600)
        before = db.count_email_failures("no-write@example.com", 3600)
        allowed, reason, _ = reserve_login_attempt(
            "no-write@example.com", "10.0.0.3",
            max_email_failures=2, email_window_seconds=3600,
            max_ip_failures=100, ip_window_seconds=3600,
            max_global_failures=1000, global_window_seconds=3600)
        self.assertFalse(allowed)
        self.assertEqual(reason, "email")
        self.assertEqual(db.count_email_failures("no-write@example.com", 3600), before)

    def test_unknown_scope_is_rejected(self):
        """计数维度只接受白名单里的名字（不允许把 SQL 片段传进来）。"""
        with self.assertRaises(KeyError):
            db.auth._count_and_last(None, "1=1; DROP TABLE user", (), 0)


class ProgressiveBackoffTests(ElenvindTestCase):
    """达到阈值后是**冷却**而不是永久锁定；等待时长随失败次数增长。"""

    def _attempt(self, when, *, limit=2, email="backoff@example.com"):
        return reserve_login_attempt(
            email, "10.0.0.9",
            max_email_failures=limit, email_window_seconds=24 * 3600,
            max_ip_failures=1000, ip_window_seconds=3600,
            max_global_failures=1000, global_window_seconds=3600,
            cooldown_base_seconds=60, cooldown_max_seconds=24 * 3600,
            attempted_at=when)

    def test_cooldown_grows_and_then_expires(self):
        now = time.time()
        # 前 2 次（阈值 2）放行；第 3 次进入冷却
        self.assertTrue(self._attempt(now - 100)[0])
        self.assertTrue(self._attempt(now - 90)[0])
        allowed, reason, retry_after = self._attempt(now - 80)
        self.assertFalse(allowed)
        self.assertEqual(reason, "email")
        self.assertLessEqual(retry_after, COOLDOWN_BASE_SECONDS)
        self.assertGreaterEqual(retry_after, 1)

        # 冷却期内仍然被拦
        allowed, _, retry_after2 = self._attempt(now - 80 + 10)
        self.assertFalse(allowed)
        self.assertLess(retry_after2, retry_after, "等待时间应随时间递减")

        # 冷却结束（超过 base 秒）后放行 —— 不是 24 小时硬锁
        allowed, _, _ = self._attempt(now - 80 + COOLDOWN_BASE_SECONDS + 1)
        self.assertTrue(allowed, "冷却结束后正常用户必须能登录（不能是硬锁 24h）")

        # 计数继续增长时冷却随之变长（比第一次的等待更久）
        allowed, _, retry_after3 = self._attempt(now)
        self.assertFalse(allowed)
        self.assertGreater(retry_after3, COOLDOWN_BASE_SECONDS,
                           f"冷却没有随失败次数增长：{retry_after3}")

    def test_hard_lock_window_is_bounded_by_the_window_not_by_the_cooldown(self):
        """窗口过期后计数归零，冷却随之消失（不存在"永久拉黑"）。"""
        old = time.time() - 25 * 3600
        for _ in range(5):
            self._attempt(old, limit=2, email="window@example.com")
        allowed, _, _ = self._attempt(
            time.time(),
            limit=2, email="window@example.com")
        self.assertTrue(allowed, "窗口外的失败不应继续影响当前判定")


class RetryAfterHeaderTests(ElenvindTestCase):
    """HTTP 层：被限流时下发 `Retry-After`，文案给出等待时长。"""

    def test_retry_after_header_and_message(self):
        from elenvind.modules.auth import routes as auth_routes

        limits = dict(auth_routes.DEFAULT_LOGIN_LIMITS)
        limits["max_email_failures"] = 1
        limits["email_window_seconds"] = 3600
        self._config["login_limits"] = limits
        self.create_user(email="retry@example.com")

        self.login("retry@example.com", "wrong")          # 第 1 次：占位
        response = self.login("retry@example.com", "wrong")   # 第 2 次：进入冷却

        def _text(value):
            return value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else value

        headers = {_text(name).lower(): _text(value) for name, value in response.headers}
        self.assertIn("retry-after", headers, headers)
        self.assertGreaterEqual(int(headers["retry-after"]), 1)
        self.assertIn("Too many failed attempts for this account", response.text)
        self.assertIn("minute", response.text, "文案必须给出可读的等待时长")

    def test_lock_does_not_leak_account_existence(self):
        """存在与不存在的邮箱在限流提示上完全一致（不暴露账号是否存在）。"""
        from elenvind.modules.auth import routes as auth_routes

        limits = dict(auth_routes.DEFAULT_LOGIN_LIMITS)
        limits["max_email_failures"] = 1
        limits["email_window_seconds"] = 3600
        self._config["login_limits"] = limits
        self.create_user(email="exists@example.com")

        self.login("exists@example.com", "wrong")
        known = self.login("exists@example.com", "wrong")
        self.login("ghost-user@example.com", "wrong")
        unknown = self.login("ghost-user@example.com", "wrong")
        self.assertIn("Too many failed attempts for this account", known.text)
        self.assertIn("Too many failed attempts for this account", unknown.text)


class LoginAttemptsIndexTests(ElenvindTestCase):
    """schema v5：全站计数必须走索引（不再全表扫描）。"""

    def test_schema_version_is_at_least_five(self):
        """v5 引入了时间索引；后续版本（v6 涂黑迁移）只会更高。"""
        self.assertGreaterEqual(db.SCHEMA_VERSION, 5)

    def test_time_index_exists(self):
        with db.connect() as conn:
            names = {row["name"] for row in
                     conn.execute("PRAGMA index_list('login_attempts')")}
        self.assertIn("idx_login_attempts_time", names, names)

    def test_global_window_query_uses_the_index(self):
        """EXPLAIN QUERY PLAN：global 计数的过滤应当用索引而不是 SCAN。"""
        with db.connect() as conn:
            plan = " ".join(
                str(row["detail"]) for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) AS cnt, MAX(attempted_at) "
                    "FROM login_attempts WHERE success = 0 AND attempted_at > ?",
                    (time.time() - 900,)))
        self.assertIn("idx_login_attempts_time", plan,
                      f"global 计数没有用上时间索引：{plan}")
        self.assertNotIn("SCAN login_attempts", plan, plan)


class MigrationV5Tests(ElenvindTestCase):
    def test_legacy_database_is_upgraded_to_v5(self):
        """v4 老库启动后应当自动补上时间索引（并继续迁移到当前版本）。"""
        path = self.tmpdir / "legacy-v4.db"
        raw = sqlite3.connect(str(path))
        raw.execute("CREATE TABLE login_attempts (id INTEGER PRIMARY KEY, email TEXT, "
                    "ip TEXT, attempted_at REAL, success INTEGER DEFAULT 0)")
        raw.execute("PRAGMA user_version=4")
        raw.commit()
        raw.close()

        had = db_connection.DB_PATH
        db_connection.DB_PATH = path
        try:
            db.init_db()
            with db.connect() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                names = {row["name"] for row in
                         conn.execute("PRAGMA index_list('login_attempts')")}
        finally:
            db_connection.DB_PATH = had
        self.assertEqual(version, db.SCHEMA_VERSION)
        self.assertIn("idx_login_attempts_time", names)


if __name__ == "__main__":
    unittest.main()
