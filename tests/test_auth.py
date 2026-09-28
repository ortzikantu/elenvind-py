"""认证与会话测试：登录、枚举、限流、会话固定、登出、改密、删号、注册、rehash。"""
import sqlite3
import unittest

from tests.support import ElenvindTestCase

from elenvind.db_base import get_connection
from elenvind.db_login import count_email_failures
from elenvind.db_session import get_session_user
from elenvind.db_user import get_user_by_email, get_user_by_id
from elenvind.security import hash_password, password_needs_rehash, verify_password


class LoginTests(ElenvindTestCase):
    def test_successful_login_issues_session(self):
        user_id, password = self.create_user(email="a@example.com")
        session, _ = self.login_ok("a@example.com", password)
        self.assertEqual(get_session_user(session), user_id)

    def test_login_is_case_insensitive_for_email(self):
        self.create_user(email="Mixed@Example.com")
        session, _ = self.login_ok("mixed@example.com", "correct horse battery")
        self.assertTrue(session)

    def test_wrong_password_is_rejected_without_session(self):
        self.create_user(email="a@example.com")
        response = self.login("a@example.com", "wrong password")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.app.set_cookie_value(response, self.session_cookie_name()), "")
        self.assertEqual(count_email_failures("a@example.com", 900), 1)

    def test_unknown_email_looks_identical(self):
        import re

        self.create_user(email="known@example.com")
        unknown = self.login("nobody@example.com", "correct horse battery")
        wrong = self.login("known@example.com", "definitely wrong")
        self.assertEqual(unknown.status, wrong.status)
        # 两条路径的可见提示一致（不泄露账号是否存在）
        self.assertIn("Invalid email or password.", unknown.text)
        self.assertIn("Invalid email or password.", wrong.text)
        # CSRF 令牌每次随机，比较时剔除
        strip = lambda text: re.sub(r'name="csrf_token" value="[^"]*"', "", text)
        self.assertEqual(strip(unknown.text), strip(wrong.text))

    def test_unknown_email_still_runs_a_password_verification(self):
        """存在与不存在的账号都必须执行一次密码校验（等量计算，弱化计时枚举）。"""
        from elenvind import view_login
        calls = []
        original = view_login.verify_password

        def spy(password, stored_hash):
            calls.append(stored_hash)
            return original(password, stored_hash)

        view_login.verify_password = spy
        try:
            self.login("ghost@example.com", "whatever123")
        finally:
            view_login.verify_password = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], view_login._DUMMY_PASSWORD_HASH)

    def test_login_rate_limit_by_email(self):
        self.create_user(email="lock@example.com")
        for _ in range(5):
            self.login("lock@example.com", "wrong")
        response = self.login("lock@example.com", "wrong")
        self.assertIn("Too many failed attempts for this account", response.text)

    def test_login_rate_limit_by_ip(self):
        from elenvind import view_login
        limits = dict(view_login.DEFAULT_LOGIN_LIMITS)
        limits["max_ip_failures"] = 3
        self._config["login_limits"] = limits
        for _ in range(3):
            self.login(f"user{_ }@example.com", "wrong")
        response = self.login("another@example.com", "wrong")
        self.assertIn("Too many failed attempts from this address", response.text)

    def test_login_rate_limit_global(self):
        from elenvind import view_login
        limits = dict(view_login.DEFAULT_LOGIN_LIMITS)
        limits["max_global_failures"] = 2
        self._config["login_limits"] = limits
        self.login("g1@example.com", "wrong")
        self.login("g2@example.com", "wrong")
        response = self.login("g3@example.com", "wrong")
        self.assertIn("Too many failed attempts. Please try again later.", response.text)

    def test_successful_login_clears_failure_history(self):
        self.create_user(email="clear@example.com")
        self.login("clear@example.com", "wrong")
        self.assertEqual(count_email_failures("clear@example.com", 900), 1)
        self.login_ok("clear@example.com", "correct horse battery")
        self.assertEqual(count_email_failures("clear@example.com", 900), 0)

    def test_session_fixation_is_prevented(self):
        """登录必须换发全新会话，并作废旧会话。"""
        user_id, password = self.create_user(email="fix@example.com")
        old_session, _ = self.login_ok("fix@example.com", password)
        new_session, _ = self.login_ok("fix@example.com", password)
        self.assertNotEqual(old_session, new_session)
        self.assertIsNone(get_session_user(old_session))
        self.assertEqual(get_session_user(new_session), user_id)

    def test_expired_session_is_rejected_and_removed(self):
        from elenvind.db_session import create_session
        user_id, _ = self.create_user(email="exp@example.com")
        token = create_session(user_id, days=-1)   # 已过期
        self.assertIsNone(get_session_user(token))
        with get_connection() as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM session WHERE token = ?",
                                     (token,)).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_forged_session_token_is_rejected(self):
        self.create_user(email="f@example.com")
        response = self.app.request("GET", "/user", cookies={"session": "A" * 43})
        self.assertNotIn("f@example.com", response.text)

    def test_password_is_rehashed_on_login_when_legacy(self):
        """历史格式哈希在登录成功后必须被透明升级为新格式（渐进式 rehash）。"""
        import hashlib
        from elenvind.db_user import update_user_password

        user_id, password = self.create_user(email="legacy@example.com")
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        update_user_password(user_id, f"{salt.hex()}${digest.hex()}")

        self.login_ok("legacy@example.com", password)
        stored = get_user_by_id(user_id)["password"]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertFalse(password_needs_rehash(stored))
        self.assertTrue(verify_password(password, stored))

    def test_rehash_not_triggered_for_current_format(self):
        user_id, password = self.create_user(email="cur@example.com")
        before = get_user_by_id(user_id)["password"]
        self.login_ok("cur@example.com", password)
        self.assertEqual(get_user_by_id(user_id)["password"], before)


class LogoutTests(ElenvindTestCase):
    def test_logout_deletes_session_and_clears_cookie(self):
        user_id, password = self.create_user(email="out@example.com")
        session, csrf = self.login_ok("out@example.com", password)
        response = self.app.request("POST", "/logout",
                                    form={"csrf_token": csrf},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/")
        self.assertIsNone(get_session_user(session))
        self.assertEqual(self.app.set_cookie_value(response, "session"), "")

    def test_logout_requires_csrf(self):
        user_id, password = self.create_user(email="out2@example.com")
        session, csrf = self.login_ok("out2@example.com", password)
        response = self.app.request("POST", "/logout", form={},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 400)
        self.assertEqual(get_session_user(session), user_id)

    def test_logout_without_session_is_harmless(self):
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/logout",
                                    form={"csrf_token": csrf},
                                    cookies={self.csrf_cookie_name(): csrf})
        self.assertEqual(response.status, 302)


class PasswordChangeTests(ElenvindTestCase):
    def _change_password(self, session, csrf, old, new, confirm=None):
        return self.app.request("POST", "/user",
                                form={"csrf_token": csrf, "action": "change_password",
                                      "old_password": old, "new_password": new,
                                      "confirm_password": confirm if confirm is not None else new},
                                cookies=self.app_cookies(session=session, csrf=csrf))

    def test_password_change_invalidates_all_sessions(self):
        import re

        user_id, password = self.create_user(email="pw@example.com")
        # 先建第二个会话，再跑主会话（登录会作废该账号的其它会话）
        stale_session, _ = self.login_ok("pw@example.com", password)
        session, csrf = self.login_ok("pw@example.com", password)
        self.assertIsNone(get_session_user(stale_session))
        self.assertEqual(get_session_user(session), user_id)

        response = self._change_password(session, csrf, password, "brand new password")
        self.assertEqual(response.status, 302, re.sub(r'name="csrf_token" value="[^"]*"', "", response.text)[:400])
        self.assertEqual(response.header("location"), "/login")
        self.assertIsNone(get_session_user(session))
        self.assertTrue(verify_password("brand new password",
                                        get_user_by_id(user_id)["password"]))
        self.assertFalse(verify_password(password, get_user_by_id(user_id)["password"]))

    def test_password_change_invalidates_other_sessions_too(self):
        user_id, password = self.create_user(email="pw5@example.com")
        other_session, _ = self.login_ok("pw5@example.com", password)
        session, csrf = self.login_ok("pw5@example.com", password)
        self.assertIsNone(get_session_user(other_session))   # 登录已作废旧会话

        # 用当前会话改密后，其它会话（这里重新发一个）必须一并失效
        session_before, _ = self.login_ok("pw5@example.com", password)
        session_now, csrf_now = self.login_ok("pw5@example.com", password)
        self.assertIsNone(get_session_user(session_before))

        response = self._change_password(session_now, csrf_now, password, "another password 9")
        self.assertEqual(response.status, 302)
        self.assertIsNone(get_session_user(session_now))

    def test_wrong_old_password_is_rejected(self):
        user_id, password = self.create_user(email="pw2@example.com")
        session, csrf = self.login_ok("pw2@example.com", password)
        response = self._change_password(session, csrf, "not my password", "new password 123")
        self.assertEqual(response.status, 200)
        self.assertIn("Current password is incorrect", response.text)
        self.assertEqual(get_session_user(session), user_id)

    def test_mismatched_confirmation_is_rejected(self):
        user_id, password = self.create_user(email="pw3@example.com")
        session, csrf = self.login_ok("pw3@example.com", password)
        response = self._change_password(session, csrf, password, "new password 123", "other")
        self.assertIn("Passwords do not match", response.text)

    def test_too_short_password_is_rejected(self):
        user_id, password = self.create_user(email="pw4@example.com")
        session, csrf = self.login_ok("pw4@example.com", password)
        response = self._change_password(session, csrf, password, "short")
        self.assertIn("Password must be 8-128 characters", response.text)


class DeleteAccountTests(ElenvindTestCase):
    def test_delete_account_logically_removes_user(self):
        user_id, password = self.create_user(email="del@example.com")
        session, csrf = self.login_ok("del@example.com", password)
        response = self.app.request("POST", "/user",
                                    form={"csrf_token": csrf, "action": "delete_account",
                                          "password_confirm": password},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/")
        self.assertIsNone(get_user_by_id(user_id))
        self.assertIsNone(get_user_by_email("del@example.com"))
        self.assertIsNone(get_session_user(session))

    def test_delete_requires_correct_password(self):
        user_id, password = self.create_user(email="del2@example.com")
        session, csrf = self.login_ok("del2@example.com", password)
        response = self.app.request("POST", "/user",
                                    form={"csrf_token": csrf, "action": "delete_account",
                                          "password_confirm": "nope nope nope"},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Password is incorrect", response.text)
        self.assertIsNotNone(get_user_by_id(user_id))


class RegistrationTests(ElenvindTestCase):
    def _register(self, nickname="Bob", email="bob@example.com",
                  password="password-123", confirm=None, csrf=None):
        token = csrf or self.fetch_csrf()
        return self.app.request("POST", "/register",
                                form={"csrf_token": token, "nickname": nickname,
                                      "email": email, "password": password,
                                      "confirm_password": confirm if confirm is not None else password},
                                cookies={self.csrf_cookie_name(): token})

    def test_successful_registration(self):
        response = self._register()
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/login")
        user = get_user_by_email("bob@example.com")
        self.assertIsNotNone(user)
        self.assertEqual(user["nickname"], "Bob")

    def test_duplicate_email_reports_normal_error_not_500(self):
        self._register()
        response = self._register(nickname="Bob2")
        self.assertEqual(response.status, 200)
        self.assertIn("Registration failed", response.text)

    def test_concurrent_duplicate_registration_races_are_handled(self):
        """绕过视图查重、直接并发插同一邮箱：UNIQUE 约束必须给出可读错误而非 500。"""
        from elenvind.view_register import _validate
        from elenvind.db_user import create_user

        # 模拟"两个请求都通过了查重，然后一起 INSERT"
        create_user("First", "race@example.com", hash_password("password-123"))
        reason, fields = _validate({"nickname": "Second", "email": "race@example.com",
                                    "password": "password-123",
                                    "confirm_password": "password-123"})
        self.assertIsNone(reason)
        with self.assertRaises(sqlite3.IntegrityError):
            create_user("Second", "race@example.com", hash_password("password-123"))

    def test_duplicate_registration_after_integrity_error_is_friendly(self):
        import elenvind.view_register as view_register_module

        token = self.fetch_csrf()
        # 让 SELECT 查重"看不到"已存在用户，强制走 INSERT 路径触发 IntegrityError
        original = view_register_module.get_user_by_email
        view_register_module.get_user_by_email = lambda email: None
        try:
            self._register(email="dupe@example.com", csrf=token)
            response = self._register(nickname="Other", email="dupe@example.com", csrf=token)
        finally:
            view_register_module.get_user_by_email = original
        self.assertEqual(response.status, 200)
        self.assertIn("Registration failed", response.text)
        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM user WHERE email = ?",
                                 ("dupe@example.com",)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_password_mismatch(self):
        response = self._register(confirm="different-password")
        self.assertIn("Passwords do not match", response.text)
        self.assertIsNone(get_user_by_email("bob@example.com"))

    def test_short_password_rejected(self):
        response = self._register(password="short", confirm="short")
        self.assertIn("Password must be 8-128 characters", response.text)

    def test_missing_fields_rejected(self):
        response = self._register(nickname="", email="")
        self.assertIn("All fields are required", response.text)

    def test_email_normalized_to_lowercase(self):
        self._register(email="MiXeD@Example.COM")
        self.assertIsNotNone(get_user_by_email("mixed@example.com"))

    def test_registration_can_be_disabled(self):
        self._config["registration_enabled"] = False
        response = self._register()
        self.assertEqual(response.status, 200)
        self.assertIn("Registration is currently closed", response.text)
        self.assertIsNone(get_user_by_email("bob@example.com"))

    def test_registration_is_rate_limited_by_ip(self):
        self._config["register_limits"] = {"max_per_ip": 2, "window_seconds": 3600}
        self._register(email="one@example.com")
        self._register(email="two@example.com")
        response = self._register(email="three@example.com")
        self.assertEqual(response.status, 200)
        self.assertIn("Too many registration attempts", response.text)
        self.assertIsNone(get_user_by_email("three@example.com"))

    def test_registration_requires_csrf(self):
        response = self.app.request("POST", "/register",
                                    form={"nickname": "x", "email": "x@example.com",
                                          "password": "password-123",
                                          "confirm_password": "password-123"})
        self.assertEqual(response.status, 400)
        self.assertIsNone(get_user_by_email("x@example.com"))


class AccountPageTests(ElenvindTestCase):
    def test_anonymous_user_page_renders_sign_in_prompt(self):
        response = self.app.request("GET", "/user")
        self.assertEqual(response.status, 200)
        self.assertIn("You are not logged in", response.text)
        self.assertIsNone(response.header("set-cookie"))

    def test_anonymous_post_to_user_page_does_not_crash(self):
        response = self.app.request("POST", "/user", form={"action": "update_profile"},
                                    headers={}, cookies=None)
        self.assertEqual(response.status, 400)   # 统一 CSRF 闸门先拒绝

    def test_logged_in_user_page_shows_profile(self):
        user_id, password = self.create_user(nickname="Carol", email="carol@example.com")
        session, csrf = self.login_ok("carol@example.com", password)
        response = self.app.request("GET", "/user",
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Carol", response.text)
        self.assertIn("carol@example.com", response.text)

    def test_profile_update_requires_password_to_change_email(self):
        user_id, password = self.create_user(nickname="Dave", email="dave@example.com")
        session, csrf = self.login_ok("dave@example.com", password)
        response = self.app.request("POST", "/user",
                                    form={"csrf_token": csrf, "action": "update_profile",
                                          "nickname": "Dave", "email": "new@example.com",
                                          "current_password": ""},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Current password is required to change email", response.text)
        self.assertEqual(get_user_by_id(user_id)["email"], "dave@example.com")

    def test_email_collision_reports_error(self):
        self.create_user(nickname="Other", email="taken@example.com")
        user_id, password = self.create_user(nickname="Erin", email="erin@example.com")
        session, csrf = self.login_ok("erin@example.com", password)
        response = self.app.request("POST", "/user",
                                    form={"csrf_token": csrf, "action": "update_profile",
                                          "nickname": "Erin", "email": "taken@example.com",
                                          "current_password": password},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Email already in use", response.text)
        self.assertEqual(get_user_by_id(user_id)["email"], "erin@example.com")

    def test_nickname_change_is_rate_limited(self):
        user_id, password = self.create_user(nickname="Frank", email="frank@example.com")
        session, csrf = self.login_ok("frank@example.com", password)
        first = self.app.request("POST", "/user",
                                 form={"csrf_token": csrf, "action": "update_profile",
                                       "nickname": "Franklin", "email": "frank@example.com",
                                       "current_password": ""},
                                 cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Profile updated successfully", first.text)
        second = self.app.request("POST", "/user",
                                  form={"csrf_token": csrf, "action": "update_profile",
                                        "nickname": "Frank2", "email": "frank@example.com",
                                        "current_password": ""},
                                  cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Nickname can only be changed once per year", second.text)

    def test_unknown_action_is_reported(self):
        user_id, password = self.create_user(email="g@example.com")
        session, csrf = self.login_ok("g@example.com", password)
        response = self.app.request("POST", "/user",
                                    form={"csrf_token": csrf, "action": "drop_tables"},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertIn("Unknown action", response.text)

    def test_profile_fields_are_escaped(self):
        user_id, password = self.create_user(
            nickname='<script>alert(1)</script>', email="xss@example.com")
        session, csrf = self.login_ok("xss@example.com", password)
        response = self.app.request("GET", "/user",
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)


if __name__ == "__main__":
    unittest.main()
