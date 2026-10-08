"""会话过期机制的测试。

覆盖验收四条：
1. 手动改 created_at 超过绝对过期 -> 会话无效；
2. 手动改 last_seen 超过滑动过期 -> 会话无效；
3. 正常活跃会话不会被误判；
4. 启动清理能删除过期会话行。
另外覆盖：滑动窗口刷新、0 = 不过期、配置校验、迁移回填。
"""
import sys
import time
import unittest

sys.path.insert(0, ".")

from tests.support import ElenvindTestCase

from elenvind.db import connect
from elenvind.db.session import (
    DEFAULT_ABSOLUTE_DAYS,
    DEFAULT_IDLE_DAYS,
    cleanup_expired_sessions,
    create_session,
    delete_session,
    get_session_user,
)

DAY = 86400


def age_session(token, *, created_days_ago=None, idle_days_ago=None,
                created_at=None, last_seen=None):
    """直接改库里的时间戳，模拟"会话已经很老 / 很久没用"。"""
    now = time.time()
    with connect() as conn:
        if created_days_ago is not None:
            conn.execute("UPDATE session SET created_at = ? WHERE token = ?",
                         (now - created_days_ago * DAY, token))
        if idle_days_ago is not None:
            conn.execute("UPDATE session SET last_seen = ? WHERE token = ?",
                         (now - idle_days_ago * DAY, token))
        if created_at is not None:
            conn.execute("UPDATE session SET created_at = ? WHERE token = ?",
                         (created_at, token))
        if last_seen is not None:
            conn.execute("UPDATE session SET last_seen = ? WHERE token = ?",
                         (last_seen, token))
        conn.commit()


def session_timestamps(token):
    with connect() as conn:
        row = conn.execute(
            "SELECT created_at, last_seen FROM session WHERE token = ?",
            (token,)).fetchone()
    return None if row is None else (row["created_at"], row["last_seen"])


def session_rows():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]


class AbsoluteExpiryTests(ElenvindTestCase):
    """验收 1：绝对过期。"""

    def test_session_past_absolute_window_is_invalid(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        self.assertEqual(get_session_user(token), user_id)      # 刚建，有效
        # 超过绝对过期（默认 30 天）
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 1)
        self.assertIsNone(get_session_user(token))

    def test_expired_row_is_deleted_not_just_ignored(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 1)
        get_session_user(token)
        self.assertIsNone(session_timestamps(token))
        self.assertEqual(session_rows(), 0)

    def test_session_just_inside_absolute_window_is_valid(self):
        """边界：还没到期就必须有效（不能提前踢人）。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS - 1,
                    idle_days_ago=1)
        self.assertEqual(get_session_user(token), user_id)

    def test_exact_absolute_boundary_is_expired(self):
        """精确边界：`now - created_at >= 绝对窗口` 时必须过期。

        现有测试只用 ±1 天，把比较符从 `>=` 改成 `>` 依然全绿。
        这里把时间精确钉在"正好等于窗口"与"差一秒"两侧：

        - 正好 30.000000 天 -> 过期（`>=` 生效）
        - 30 天差 1 秒      -> 有效
        """
        from elenvind.db.session import DAY_SECONDS, _is_expired

        now = 1_000_000_000.0
        window = DEFAULT_ABSOLUTE_DAYS * DAY_SECONDS
        fresh_idle = now - 1.0            # idle 维度不干扰

        with self.subTest(case="exactly at the window"):
            row = {"created_at": now - window, "last_seen": fresh_idle}
            self.assertTrue(
                _is_expired(row, DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now),
                "正好等于绝对窗口时必须判定为已过期（>= 语义）")

        with self.subTest(case="one second before the window"):
            row = {"created_at": now - window + 1.0, "last_seen": fresh_idle}
            self.assertFalse(
                _is_expired(row, DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now),
                "差一秒到期时不该被判为过期")

        with self.subTest(case="one second past the window"):
            row = {"created_at": now - window - 1.0, "last_seen": fresh_idle}
            self.assertTrue(
                _is_expired(row, DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

    def test_exact_idle_boundary_is_expired(self):
        """精确边界：空闲维度同样是 `>=`。"""
        from elenvind.db.session import DAY_SECONDS, _is_expired

        now = 1_000_000_000.0
        window = DEFAULT_IDLE_DAYS * DAY_SECONDS
        fresh_created = now - 1.0         # absolute 维度不干扰

        with self.subTest(case="exactly at the window"):
            row = {"created_at": fresh_created, "last_seen": now - window}
            self.assertTrue(
                _is_expired(row, DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

        with self.subTest(case="one second before the window"):
            row = {"created_at": fresh_created, "last_seen": now - window + 1.0}
            self.assertFalse(
                _is_expired(row, DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

    def test_absolute_expiry_is_independent_of_activity(self):
        """一直活跃也不能突破绝对过期窗口。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 5)
        # last_seen 是"刚刚"，仍然应判过期
        age_session(token, idle_days_ago=0)
        self.assertIsNone(get_session_user(token))


class IdleExpiryTests(ElenvindTestCase):
    """验收 2：滑动过期。"""

    def test_session_idle_past_window_is_invalid(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, idle_days_ago=DEFAULT_IDLE_DAYS + 1)
        self.assertIsNone(get_session_user(token))

    def test_idle_expired_row_is_deleted(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, idle_days_ago=DEFAULT_IDLE_DAYS + 1)
        get_session_user(token)
        self.assertIsNone(session_timestamps(token))

    def test_activity_refreshes_last_seen(self):
        """有效访问必须刷新 last_seen（滑动窗口的核心行为）。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, idle_days_ago=DEFAULT_IDLE_DAYS - 2)
        before = session_timestamps(token)[1]
        self.assertEqual(get_session_user(token), user_id)
        after = session_timestamps(token)[1]
        self.assertGreater(after, before)
        # 刷新后剩余闲置额度应该又变回接近满额
        self.assertLess(time.time() - after, 5)

    def test_repeated_activity_keeps_session_alive(self):
        """验收 3：持续活跃的会话不会被误判过期。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        for _ in range(5):
            # 每次都把闲置时间推到窗口边缘，再访问一次把它拉回来
            age_session(token, idle_days_ago=DEFAULT_IDLE_DAYS - 1)
            self.assertEqual(get_session_user(token), user_id)
        self.assertEqual(get_session_user(token), user_id)

    def test_fresh_session_is_valid(self):
        """验收 3：刚建的会话当然有效。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        self.assertEqual(get_session_user(token), user_id)
        self.assertEqual(get_session_user(token), user_id)


class DisabledExpiryTests(ElenvindTestCase):
    """0 = 该维度不过期（显式选择，文档里写明风险）。"""

    def test_zero_absolute_days_never_expires_by_age(self):
        self._config["session_absolute_days"] = 0
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=10000, idle_days_ago=1)
        self.assertEqual(get_session_user(token), user_id)

    def test_zero_idle_days_never_expires_by_inactivity(self):
        self._config["session_idle_days"] = 0
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=1, idle_days_ago=10000)
        self.assertEqual(get_session_user(token), user_id)

    def test_both_zero_disables_expiry_entirely(self):
        self._config["session_absolute_days"] = 0
        self._config["session_idle_days"] = 0
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=99999, idle_days_ago=99999)
        self.assertEqual(get_session_user(token), user_id)
        # 两个维度都关了：清理任务不该删掉任何东西
        self.assertEqual(cleanup_expired_sessions(), 0)
        self.assertEqual(session_rows(), 1)

    def test_custom_thresholds_are_honoured(self):
        self._config["session_absolute_days"] = 2
        self._config["session_idle_days"] = 1
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=3, idle_days_ago=0)
        self.assertIsNone(get_session_user(token))       # 绝对过期
        token2 = create_session(user_id)
        age_session(token2, idle_days_ago=2)
        self.assertIsNone(get_session_user(token2))      # 滑动过期


class MalformedTimestampTests(ElenvindTestCase):
    """时间戳被写坏时失败关闭（不能变成"永不过期"）。

    表定义里 created_at / last_seen 都是 NOT NULL，所以正常路径写不进 NULL；
    这里直接对判定函数做单元测试，锁住"失败关闭"这条不变式——
    万一将来 schema 放宽、或有人手工改库，判定仍然是否过期而不是放行。
    """

    def test_null_timestamps_are_treated_as_expired(self):
        from elenvind.db.session import _is_expired
        now = time.time()
        self.assertTrue(_is_expired({"created_at": None, "last_seen": now},
                                    DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))
        self.assertTrue(_is_expired({"created_at": now, "last_seen": None},
                                    DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))
        self.assertTrue(_is_expired({"created_at": None, "last_seen": None},
                                    DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

    def test_schema_enforces_not_null(self):
        """schema 层就不允许写 NULL（比运行时判定更早拦住）。"""
        import sqlite3
        user_id, _ = self.create_user()
        token = create_session(user_id)
        for column in ("created_at", "last_seen"):
            with self.subTest(column=column):
                with connect() as conn:
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            f"UPDATE session SET {column} = NULL WHERE token = ?",
                            (token,))
        # 原行未被破坏，仍可正常使用
        self.assertEqual(get_session_user(token), user_id)

    def test_valid_timestamps_are_not_flagged(self):
        from elenvind.db.session import _is_expired
        now = time.time()
        self.assertFalse(_is_expired({"created_at": now - 60,
                                      "last_seen": now - 30},
                                     DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

    def test_non_numeric_timestamps_are_treated_as_expired(self):
        """回归：SQLite 是动态类型，REAL 列里可能存着 TEXT。

        旧实现只判了 NULL，遇到字符串会直接 `TypeError` 冒到 App 层变成 500 ——
        于是每个带该 Cookie 的请求都 500，而不是干脆当作未登录（用户重新登录即可）。
        """
        from elenvind.db.session import _is_expired
        now = time.time()
        for bad in ("not-a-number", "2026-01-01T00:00:00", b"12345", "",
                    [], {}, object()):
            with self.subTest(value=repr(bad)):
                self.assertTrue(
                    _is_expired({"created_at": bad, "last_seen": now},
                                DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now),
                    f"{bad!r} 应被视为已过期（失败关闭）")
                self.assertTrue(
                    _is_expired({"created_at": now, "last_seen": bad},
                                DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

    def test_nan_and_inf_are_treated_as_expired(self):
        from elenvind.db.session import _is_expired
        now = time.time()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                self.assertTrue(
                    _is_expired({"created_at": bad, "last_seen": now},
                                DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS, now))

    def test_numeric_strings_are_accepted(self):
        """看起来像数字的字符串是 SQLite 动态类型的正常产物，应当可用。"""
        from elenvind.db.session import _as_timestamp
        self.assertEqual(_as_timestamp("1234.5"), 1234.5)
        self.assertEqual(_as_timestamp(1234), 1234.0)
        self.assertIsNone(_as_timestamp(True), "bool 不是时间戳")


class StartupCleanupTests(ElenvindTestCase):
    """验收 4：启动清理能删除过期会话行。"""

    def test_cleanup_removes_expired_sessions(self):
        user_id, _ = self.create_user()
        fresh = create_session(user_id)
        by_age = create_session(user_id)
        by_idle = create_session(user_id)
        age_session(by_age, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 1)
        age_session(by_idle, idle_days_ago=DEFAULT_IDLE_DAYS + 1)
        self.assertEqual(session_rows(), 3)

        removed = cleanup_expired_sessions()
        self.assertEqual(removed, 2)
        self.assertEqual(session_rows(), 1)
        # 未过期的那个必须留下并且仍然有效
        self.assertEqual(get_session_user(fresh), user_id)

    def test_cleanup_is_idempotent(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 1)
        self.assertEqual(cleanup_expired_sessions(), 1)
        self.assertEqual(cleanup_expired_sessions(), 0)
        self.assertEqual(session_rows(), 0)

    def test_cleanup_keeps_active_sessions(self):
        user_id, _ = self.create_user()
        tokens = [create_session(user_id) for _ in range(5)]
        self.assertEqual(cleanup_expired_sessions(), 0)
        self.assertEqual(session_rows(), 5)
        for token in tokens:
            self.assertEqual(get_session_user(token), user_id)

    def test_create_session_also_sweeps_expired(self):
        """发证时顺带清理，避免长期不重启导致表膨胀。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 1)
        create_session(user_id)                     # 触发顺带清理
        self.assertEqual(session_rows(), 1)         # 只剩新发的那个

    def test_cleanup_does_not_touch_other_tables(self):
        """清理只该动 session 表。"""
        user_id, _ = self.create_user()
        token = create_session(user_id)
        age_session(token, created_days_ago=DEFAULT_ABSOLUTE_DAYS + 1)
        cleanup_expired_sessions()
        with connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM user").fetchone()[0]
        self.assertEqual(users, 1)


class ExplicitLogoutStillWorksTests(ElenvindTestCase):
    """确保新增过期逻辑没有破坏既有的删除路径。"""

    def test_delete_session_removes_row(self):
        user_id, _ = self.create_user()
        token = create_session(user_id)
        delete_session(token)
        self.assertIsNone(get_session_user(token))
        self.assertEqual(session_rows(), 0)

    def test_unknown_token_returns_none(self):
        self.assertIsNone(get_session_user("does-not-exist"))
        self.assertIsNone(get_session_user(""))


class SessionCookieLifetimeTests(ElenvindTestCase):
    """Cookie 寿命跟随绝对过期窗口（滑动过期更短，不该把 Cookie 留更久）。"""

    def test_cookie_max_age_matches_absolute_window(self):
        from elenvind.core.session import session_cookie_max_age
        self._config["session_absolute_days"] = 30
        self.assertEqual(session_cookie_max_age(), 30 * DAY)
        self._config["session_absolute_days"] = 3
        self.assertEqual(session_cookie_max_age(), 3 * DAY)

    def test_cookie_max_age_falls_back_when_expiry_disabled(self):
        """绝对不过期时不能下发 max-age=0（那会让 Cookie 立刻失效）。"""
        from elenvind.core.security import SESSION_MAX_AGE
        from elenvind.core.session import session_cookie_max_age
        self._config["session_absolute_days"] = 0
        self.assertEqual(session_cookie_max_age(), SESSION_MAX_AGE)
        self.assertGreater(session_cookie_max_age(), 0)


class SessionExpiryConfigValidationTests(unittest.TestCase):
    """配置校验：类型与范围。"""

    def setUp(self):
        from elenvind.core import config as config_module
        from elenvind.core.templating import DEFAULT_TEMPLATES_DIR
        self.config_module = config_module
        self.base = {
            "title": "T",
            "site_url": "https://example.com",
            "articles_dir": str(DEFAULT_TEMPLATES_DIR.parent / "templates"),
        }

    def _validate(self, **overrides):
        from elenvind.core.config import validate_config
        cfg = dict(self.base)
        cfg.update(overrides)
        original = dict(self.config_module.config)
        self.config_module.config.clear()
        self.config_module.config.update(cfg)
        try:
            validate_config()
        finally:
            self.config_module.config.clear()
            self.config_module.config.update(original)

    def test_defaults_pass(self):
        self._validate()

    def test_zero_is_accepted(self):
        self._validate(session_absolute_days=0, session_idle_days=0)

    def test_reasonable_values_pass(self):
        self._validate(session_absolute_days=1, session_idle_days=1)
        self._validate(session_absolute_days=365, session_idle_days=90)

    def test_negative_is_rejected(self):
        from elenvind.core.config import ConfigError
        with self.assertRaises(ConfigError):
            self._validate(session_absolute_days=-1)
        with self.assertRaises(ConfigError):
            self._validate(session_idle_days=-1)

    def test_non_integer_is_rejected(self):
        from elenvind.core.config import ConfigError
        for bad in ("30", 1.5, None, [30]):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    self._validate(session_absolute_days=bad)

    def test_absurdly_large_is_rejected(self):
        """超过 10 年基本等于"不想过期"，应显式写 0。"""
        from elenvind.core.config import ConfigError
        with self.assertRaises(ConfigError):
            self._validate(session_absolute_days=99999)
        with self.assertRaises(ConfigError):
            self._validate(session_idle_days=99999)

    def test_shipped_configs_declare_the_keys(self):
        """两份配置都要显式写出这两个键（站长看得见才谈得上调整）。"""
        import tomllib
        from tests.support import PROJECT_ROOT
        for name in ("config.toml", "config.example.toml"):
            with open(PROJECT_ROOT / name, "rb") as handle:
                data = tomllib.load(handle)
            with self.subTest(config=name):
                self.assertIn("session_absolute_days", data)
                self.assertIn("session_idle_days", data)


if __name__ == "__main__":
    unittest.main()
