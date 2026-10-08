"""认证与会话测试：登录、枚举、限流、会话固定、登出、改密、删号、注册、rehash。"""
import sqlite3
import time
import unittest

from tests.support import ElenvindTestCase

from elenvind.db import connect
from elenvind.db.auth import count_email_failures
from elenvind.db.session import get_session_user
from elenvind.db.user import get_user_by_email, get_user_by_id
from elenvind.core.security import hash_password, password_needs_rehash, verify_password


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
        """存在与不存在的账号都必须执行一次密码校验（等量计算，弱化计时枚举）。

        未知账号走 `core.security.dummy_verify`（对哑哈希做一次真实 scrypt），
        因此这里统计的是哑校验被调用。
        """
        from elenvind.core import security as security_module

        calls = []
        original = security_module.verify_password

        def spy(password, stored_hash):
            calls.append(stored_hash)
            return original(password, stored_hash)

        security_module.verify_password = spy
        try:
            self.login("ghost@example.com", "whatever123")
        finally:
            security_module.verify_password = original
        self.assertEqual(len(calls), 1)
        # 哑校验用的哈希与真实用户哈希格式一致（同为 scrypt 自描述格式）
        self.assertTrue(str(calls[0]).startswith("scrypt$"))

    def test_login_rate_limit_by_email(self):
        self.create_user(email="lock@example.com")
        for _ in range(5):
            self.login("lock@example.com", "wrong")
        response = self.login("lock@example.com", "wrong")
        self.assertIn("Too many failed attempts for this account", response.text)

    def test_login_rate_limit_by_ip(self):
        from elenvind.modules.auth import routes as auth_routes
        limits = dict(auth_routes.DEFAULT_LOGIN_LIMITS)
        limits["max_ip_failures"] = 3
        self._config["login_limits"] = limits
        for index in range(3):
            self.login(f"user{index}@example.com", "wrong")
        response = self.login("another@example.com", "wrong")
        self.assertIn("Too many failed attempts from this address", response.text)

    def test_login_rate_limit_global(self):
        from elenvind.modules.auth import routes as auth_routes
        limits = dict(auth_routes.DEFAULT_LOGIN_LIMITS)
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
        from elenvind.db.session import create_session
        user_id, _ = self.create_user(email="exp@example.com")
        token = create_session(user_id)
        # 把创建时间推到绝对过期窗口之外（默认 30 天）
        with connect() as conn:
            conn.execute("UPDATE session SET created_at = ? WHERE token = ?",
                         (time.time() - 31 * 86400, token))
            conn.commit()
        self.assertIsNone(get_session_user(token))
        with connect() as conn:
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
        from elenvind.db.user import update_user_password

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

    def test_logout_without_session_is_rejected_cleanly(self):
        """未登录调用登出：auth 闸门统一拒绝（403），不报错也不产生副作用。"""
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/logout",
                                    form={"csrf_token": csrf},
                                    cookies={self.csrf_cookie_name(): csrf})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.app.set_cookie_value(response, "session"), "")


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

    def test_password_change_kills_a_truly_concurrent_session(self):
        """改密必须踢掉**同时存活**的另一个会话。

        ⚠️ 为什么需要这条测试：上面两条 `..._invalidates_..._sessions` 里的
        "其它会话"其实都是**登录轮换**先杀掉、再被断言的 ——
        它们证明的是"登录会清旧会话"，而不是"改密会清其他会话"。
        把 `core.session.invalidate_user_sessions()` 从改密分支里整行删掉，
        上面两条依然全绿（实测）。

        要真正观测到改密的作用，必须造出"同一账号有两个活跃会话"的状态 ——
        而 `login` 本身会清掉旧的，所以这里直接在库里落一个额外会话
        （等价于"另一个浏览器还登着"）。
        """
        from elenvind.db.session import create_session, get_session_user

        user_id, password = self.create_user(email="pw-concurrent@example.com")
        current, csrf = self.login_ok("pw-concurrent@example.com", password)
        # 绕过 login 直接落一个会话：模拟"另一个浏览器仍处于登录态"
        other = create_session(user_id)
        self.assertEqual(get_session_user(current), user_id)
        self.assertEqual(get_session_user(other), user_id)

        response = self.app.request(
            "POST", "/user",
            form={"csrf_token": csrf, "action": "change_password",
                  "old_password": password,
                  "new_password": "brand new password",
                  "confirm_password": "brand new password"},
            cookies=self.app_cookies(session=current, csrf=csrf))

        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/login")
        self.assertIsNone(get_session_user(other),
                          "改密没有踢掉另一个活跃会话")
        self.assertIsNone(get_session_user(current),
                          "改密没有踢掉当前会话")
        # 浏览器侧的 Cookie 也必须被清除（而不是留着一个失效 token）
        session_headers = [h for h in response.headers_all("set-cookie")
                           if h.startswith("session=")]
        self.assertTrue(session_headers, "改密没有下发清除 session Cookie 的指令")
        for header in session_headers:
            self.assertIn("Max-Age=0", header, header)

    def test_password_change_does_not_kill_other_accounts_sessions(self):
        """改密只影响自己：不得误伤别人的会话。"""
        from elenvind.db.session import create_session, get_session_user

        victim_id, password = self.create_user(email="victim-pw@example.com")
        bystander_id, _ = self.create_user(email="bystander@example.com")
        bystander_session = create_session(bystander_id)
        session, csrf = self.login_ok("victim-pw@example.com", password)

        response = self._change_password(session, csrf, password, "brand new password")
        self.assertEqual(response.status, 302)
        self.assertEqual(get_session_user(bystander_session), bystander_id,
                         "改密误伤了其它账号的会话")

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

    def test_squatting_the_placeholder_email_cannot_block_deletion(self):
        """回归：占位邮箱曾经是 `deleted_<id>@example.com`，可被抢注。

        user_id 是公开的（评论区渲染 `(#id)`）且连续递增，所以任何人都能在
        受害者删号前注册 `deleted_<id>@example.com`；等到受害者删号时撞
        UNIQUE 约束 -> 500 -> **账号被永久锁死删不掉**。
        """
        from elenvind.db.user import delete_user
        from elenvind.db.user import create_user as core_create_user

        victim_id, _ = self.create_user(nickname="Victim", email="victim@example.com")
        # 攻击者抢注旧实现会用的占位邮箱
        squatted = f"deleted_{victim_id}@example.com"
        core_create_user(nickname="Attacker", email=squatted,
                         password_hash=hash_password("attacker password"))

        delete_user(victim_id)          # 不得抛异常

        with connect() as conn:
            row = conn.execute(
                "SELECT email, is_deleted, nickname FROM user WHERE id = ?",
                (victim_id,)).fetchone()
        self.assertEqual(row["is_deleted"], 1)
        self.assertEqual(row["nickname"], "Ghost")
        self.assertNotEqual(row["email"], squatted, "占位邮箱不得可预测")

    def test_placeholder_email_is_random_and_unregisterable(self):
        import re
        from elenvind.db.user import delete_user

        ids = []
        for index in range(3):
            user_id, _ = self.create_user(email=f"ph{index}@example.com")
            ids.append(user_id)
        for user_id in ids:
            delete_user(user_id)

        with connect() as conn:
            emails = [row["email"] for row in conn.execute(
                "SELECT email FROM user WHERE email LIKE 'deleted+%'")]
        self.assertEqual(len(emails), 3)
        self.assertEqual(len(set(emails)), 3, "占位邮箱必须互不相同")
        for email in emails:
            with self.subTest(email=email):
                self.assertRegex(email, r"^deleted\+[0-9a-f]{32}@deleted\.invalid$")
                self.assertTrue(email.endswith(".invalid"),
                                ".invalid 是 RFC 2606 保留域，永不可注册")

    def test_double_delete_reports_instead_of_silently_succeeding(self):
        from elenvind.db.user import delete_user

        user_id, _ = self.create_user(email="twice@example.com")
        delete_user(user_id)
        with self.assertRaises(ValueError):
            delete_user(user_id)


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
        """绕过查重、直接并发插同一邮箱：UNIQUE 约束必须给出可读错误而非 500。"""
        from elenvind.db.user import create_user

        create_user("First", "race@example.com", hash_password("password-123"))
        # 模拟"两个请求都通过了查重，然后一起 INSERT"
        with self.assertRaises(sqlite3.IntegrityError):
            create_user("Second", "race@example.com", hash_password("password-123"))

        # 走真实 HTTP 路径：重复邮箱必须得到友好提示（200 + 文案），而不是 500
        token = self.fetch_csrf()
        self._register(nickname="First", email="race@example.com", csrf=token)
        response = self._register(nickname="Second", email="race@example.com", csrf=token)
        self.assertEqual(response.status, 200)
        self.assertIn("Registration failed", response.text)
        with connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM user WHERE email = ?",
                                 ("race@example.com",)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_duplicate_registration_after_integrity_error_is_friendly(self):
        import elenvind.modules.auth.routes as auth_routes

        token = self.fetch_csrf()
        # 让 SELECT 查重"看不到"已存在用户，强制走 INSERT 路径触发 IntegrityError
        original = auth_routes.get_user_by_email
        auth_routes.get_user_by_email = lambda email: None
        try:
            self._register(email="dupe@example.com", csrf=token)
            response = self._register(nickname="Other", email="dupe@example.com", csrf=token)
        finally:
            auth_routes.get_user_by_email = original
        self.assertEqual(response.status, 200)
        self.assertIn("Registration failed", response.text)
        with connect() as conn:
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
    def test_anonymous_user_page_shows_friendly_sign_in_prompt(self):
        """未登录访问 /user：渲染友好的"请先登录"页，而不是 403。"""
        response = self.app.request("GET", "/user")
        self.assertEqual(response.status, 200)
        self.assertIn("You are not logged in", response.text)
        # 提供登录入口，并带回跳地址（登录后回到本页）
        self.assertIn('href="/login?next=/user"', response.text)
        # 不得泄漏任何账号信息，也不得出现只有登录后才能看到的表单
        self.assertNotIn('name="password_confirm"', response.text)
        self.assertNotIn('name="nickname"', response.text)

    def test_anonymous_post_to_user_page_is_rejected(self):
        """未登录 + 无 CSRF 的 POST：先被 CSRF 闸门拒绝（400），无状态变更。"""
        response = self.app.request("POST", "/user", form={"action": "update_profile"},
                                    headers={}, cookies=None)
        self.assertEqual(response.status, 400)

    def test_anonymous_post_with_valid_csrf_is_forbidden(self):
        """未登录但令牌有效：auth 闸门拒绝（403）——写操作没有"跳转"可言。"""
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/user",
                                    form={"csrf_token": csrf, "action": "update_profile"},
                                    cookies={self.csrf_cookie_name(): csrf})
        self.assertEqual(response.status, 403)

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
