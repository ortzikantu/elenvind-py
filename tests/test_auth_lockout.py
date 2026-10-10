"""登录闸门 / 会话 / 口令策略。

回归重点（本次修复）：

1. **闸门命中绝不能阻断正确凭据**。修复前的顺序是"闸门命中 → 直接返回锁定提示"，
   而失败流水唯一的清空入口又是"登录成功"本身 —— 于是 5 个请求就能把任意已知
   邮箱锁死最长 24 小时，受害者拿着正确密码也进不来，站内也没有解锁入口。
2. 闸门期间的**错误**尝试仍要记账，否则计数被冻住、冷却永不加深。
3. 会话刷新按 `LAST_SEEN_REFRESH_SECONDS` 节流：静态资源不该每个请求都拿写锁。
"""
from __future__ import annotations

import unittest

from tests import support

EMAIL = "victim@example.test"
PASSWORD = "correct-horse-1"


class LoginGate(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()
        support.Session().create_account("Victim", EMAIL, PASSWORD)

    def _fail_login(self, times: int, email: str = EMAIL):
        results = []
        for _ in range(times):
            results.append(support.Session().login(email, "wrong-password"))
        return results

    def _failure_rows(self) -> int:
        return support.query(
            "SELECT COUNT(*) FROM login_attempts WHERE success = 0")[0][0]

    def test_valid_credentials_are_never_locked_out(self):
        """测试配置 max_email_failures = 3：第 4 次尝试起闸门命中。"""
        results = self._fail_login(4)
        self.assertEqual(results[-1].status, 200)
        self.assertTrue(results[-1].header("retry-after"), "the gate should be tripped")

        victim = support.Session()
        ok = victim.login(EMAIL, PASSWORD)
        self.assertEqual(ok.status, 302,
                         "correct credentials must never be blocked by the gate")
        self.assertEqual(ok.header("location"), "/")
        self.assertTrue(victim.cookies.get("session"))
        # 成功登录清空该邮箱的失败流水 → 当场自解封
        self.assertEqual(self._failure_rows(), 0)

    def test_wrong_password_at_the_gate_reports_cooldown(self):
        self._fail_login(4)
        again = support.Session().login(EMAIL, "wrong-password")
        self.assertEqual(again.status, 200)
        self.assertTrue(again.header("retry-after"))
        self.assertIn("minute", again.text().lower())
        self.assertNotIn(EMAIL, again.text())          # 不泄漏账号是否存在

    def test_gate_failures_are_still_recorded(self):
        self._fail_login(4)
        before = self._failure_rows()
        support.Session().login(EMAIL, "wrong-password")
        self.assertEqual(self._failure_rows(), before + 1,
                         "a failed attempt during the cooldown must still be counted")

    def test_successful_login_is_audited(self):
        support.Session().login(EMAIL, PASSWORD)
        self.assertEqual(
            support.query("SELECT COUNT(*) FROM login_attempts WHERE success = 1")[0][0], 1)


class Sessions(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()
        support.Session().create_account("Victim", EMAIL, PASSWORD)

    def _login_session(self) -> support.Session:
        session = support.Session()
        self.assertEqual(session.login(EMAIL, PASSWORD).status, 302)
        return session

    def test_login_rotates_the_session_token(self):
        first = self._login_session()
        token1 = first.cookies["session"]
        second = self._login_session()
        token2 = second.cookies["session"]
        self.assertNotEqual(token1, token2)
        self.assertEqual(
            support.query("SELECT COUNT(*) FROM session WHERE token=?", (token1,))[0][0], 0)
        self.assertEqual(
            support.query("SELECT COUNT(*) FROM session WHERE token=?", (token2,))[0][0], 1)

    def test_get_logout_has_no_side_effect_but_post_does(self):
        session = self._login_session()
        token = session.cookies["session"]

        self.assertEqual(session.get("/logout").status, 200)
        self.assertEqual(
            support.query("SELECT COUNT(*) FROM session WHERE token=?", (token,))[0][0], 1,
            "GET must not destroy the session")

        result = session.post("/logout", {"csrf_token": session.csrf("/logout")})
        self.assertEqual(result.status, 302)
        self.assertEqual(
            support.query("SELECT COUNT(*) FROM session WHERE token=?", (token,))[0][0], 0)
        # SimpleCookie 把空值渲染成 `session=""`，因此比较前先去掉引号
        self.assertEqual((result.cookies.get("session") or "x").strip('"'), "",
                         "the browser cookie must be cleared too")

    def test_logout_requires_csrf(self):
        session = self._login_session()
        self.assertEqual(session.post("/logout", {"csrf_token": "x" * 43}).status, 400)
        self.assertEqual(session.post("/logout", {}).status, 400)

    def test_last_seen_refresh_is_throttled(self):
        from elenvind import db

        session = self._login_session()
        token = session.cookies["session"]

        def last_seen() -> float:
            return support.query(
                "SELECT last_seen FROM session WHERE token=?", (token,))[0][0]

        fresh = last_seen()
        session.get("/")                       # 刚刷新过：不应再写库
        self.assertEqual(last_seen(), fresh)

        with db.write_tx() as conn:            # 拨回 10 分钟前
            conn.execute("UPDATE session SET last_seen = last_seen - 600 WHERE token = ?",
                         (token,))
        stale = last_seen()
        session.get("/")
        self.assertGreater(last_seen(), stale, "a stale session must be refreshed")

    def test_session_cookie_attributes(self):
        session = self._login_session()
        header = session.get("/user").all_headers("set-cookie")
        self.assertEqual(header, [])           # 普通请求不回带 Set-Cookie
        login_headers = support.Session().login(EMAIL, PASSWORD).all_headers("set-cookie")
        self.assertTrue(any("HttpOnly" in value and "SameSite=Lax" in value
                            for value in login_headers), login_headers)


class PasswordPolicy(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()

    def _register(self, nickname: str, email: str, password: str):
        return support.Session().register(nickname, email, password)

    def test_common_and_trivial_passwords_are_rejected(self):
        for index, weak in enumerate(("12345678", "aaaaaaaa", "password", "qwerty123")):
            result = self._register("Weak", f"weak{index}@example.test", weak)
            self.assertEqual(result.status, 200, weak)
            self.assertIn("weak", result.text().lower(), weak)

    def test_password_equal_to_email_or_nickname_is_rejected(self):
        self.assertIn("weak", self._register(
            "Someone", "bobsmith@example.test", "bobsmith").text().lower())
        self.assertIn("weak", self._register(
            "nickname", "other@example.test", "nickname").text().lower())

    def test_length_rules_still_apply(self):
        self.assertIn("8-128", self._register("Short", "short@example.test", "a1b2c3").text())

    def test_reasonable_password_is_accepted(self):
        self.assertEqual(self._register(
            "Good", "good@example.test", "correct-horse-1").status, 302)

    def test_password_change_uses_the_same_rule(self):
        session = support.Session()
        session.create_account("Alice", "alice@example.test", "correct-horse-1")
        result = session.post("/user", {
            "action": "change_password",
            "old_password": "correct-horse-1",
            "new_password": "12345678",
            "confirm_password": "12345678",
            "csrf_token": session.csrf("/user"),
        })
        self.assertEqual(result.status, 200)
        self.assertIn("weak", result.text().lower())


if __name__ == "__main__":
    unittest.main()
