"""日志与代理信任测试。

- 日志脱敏：密码、哈希、会话 token、CSRF token、完整 Cookie、POST body
  都不允许出现在日志里（用真实请求触发各类日志，再检查捕获到的日志记录）。
- 客户端 IP：只有直连对端在 trusted_proxies 内时才采信 X-Forwarded-For。
"""
import io
import logging
import unittest
from urllib.parse import urlencode

from tests.support import PROJECT_ROOT, ElenvindTestCase

from elenvind.core.utils import get_client_ip

#: 一旦出现在日志里就说明发生了敏感信息泄漏
SENSITIVE_MARKERS = (
    "correct horse battery",      # 明文密码
    "scrypt$",                    # 密码哈希
    "pbkdf2_sha256$",             # 旧格式密码哈希
    "csrf_token=",                # 表单里的 CSRF 值
    "set-cookie",                 # 完整 Cookie 头
    "session=",                   # 会话 Cookie
    "__Host-session=",
)


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.stream = io.StringIO()

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(record.getMessage() for record in self.records)


class LogRedactionTests(ElenvindTestCase):
    def setUp(self):
        super().setUp()
        self.handler = _CapturingHandler()
        root = logging.getLogger()
        self._previous_level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(self._restore)

    def _restore(self):
        logging.getLogger().removeHandler(self.handler)
        logging.getLogger().setLevel(self._previous_level)

    def test_no_secrets_in_logs_across_a_full_flow(self):
        self.write_article("post", "body", {"title": "T", "date": "2026-01-01"})
        csrf = self.fetch_csrf()
        cookies = self.csrf_cookies(csrf)

        # 注册 + 登录（含失败登录）
        self.app.request("POST", "/register",
                         form={"csrf_token": csrf, "nickname": "Logger",
                               "email": "logger@example.com",
                               "password": "correct horse battery",
                               "confirm_password": "correct horse battery"}, cookies=cookies)
        self.app.request("POST", "/login",
                         form={"csrf_token": csrf, "email": "logger@example.com",
                               "password": "wrong password here"}, cookies=cookies)
        login = self.app.request("POST", "/login",
                                 form={"csrf_token": csrf, "email": "logger@example.com",
                                       "password": "correct horse battery"}, cookies=cookies)
        session = self.app.set_cookie_value(login, self.session_cookie_name())
        auth = {"session": session, "csrf": csrf}

        self.app.request("GET", "/user", cookies=auth)
        self.app.request("POST", "/article/post/comment",
                         form={"csrf_token": csrf, "content": "hello"}, cookies=auth)
        self.app.request("POST", "/user",
                         form={"csrf_token": csrf, "action": "change_password",
                               "old_password": "correct horse battery",
                               "new_password": "another secret 123",
                               "confirm_password": "another secret 123"}, cookies=auth)
        self.app.request("POST", "/logout", form={"csrf_token": csrf}, cookies=auth)

        text = self.handler.text()
        self.assertTrue(self.handler.records, "expected some log records")
        for marker in SENSITIVE_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)
        for secret in ("correct horse battery", "another secret 123", "wrong password here"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)

    def test_unhandled_exception_log_does_not_leak_body_or_cookies(self):
        from elenvind.app import app

        # 先拿令牌（此时路由还是正常的），再替换首页 handler 制造未捕获异常
        csrf = self.fetch_csrf()
        target = next(route for route in app.router.routes if route.path == "/")
        original = target.handler

        def boom(request):
            raise RuntimeError("boom")

        target.handler = boom
        try:
            self.app.request("GET", "/", cookies={"session": "A" * 43, "csrf": csrf})
        finally:
            target.handler = original

        text = self.handler.text()
        self.assertIn("Unhandled exception", text)
        self.assertNotIn("A" * 43, text)
        self.assertNotIn(csrf, text)

    def test_rate_limit_logs_contain_only_non_secret_fields(self):
        self.write_article("post", "body")
        self._config["comment_limits"] = {"max_per_user": 1, "max_per_ip": 100,
                                          "window_seconds": 60}
        csrf = self.fetch_csrf()
        self.app.request("POST", "/register",
                         form={"csrf_token": csrf, "nickname": "Spammer",
                               "email": "spam@example.com",
                               "password": "correct horse battery",
                               "confirm_password": "correct horse battery"},
                         cookies=self.csrf_cookies(csrf))
        login = self.app.request("POST", "/login",
                                 form={"csrf_token": csrf, "email": "spam@example.com",
                                       "password": "correct horse battery"},
                                 cookies=self.csrf_cookies(csrf))
        session = self.app.set_cookie_value(login, self.session_cookie_name())
        auth = {"session": session, "csrf": csrf}
        for index in range(2):
            self.app.request("POST", "/article/post/comment",
                             form={"csrf_token": csrf, "content": f"c{index}"}, cookies=auth)

        text = self.handler.text()
        self.assertIn("Comment rate limit hit", text)
        self.assertNotIn("correct horse battery", text)
        for marker in SENSITIVE_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_log_call_sites_never_pass_known_secret_variables(self):
        """静态守卫：日志调用里不得出现 password / token / cookie 等变量名。"""
        import ast

        offenders = []
        banned = ("password", "token", "cookie", "secret", "hash")
        for path in sorted((PROJECT_ROOT / "elenvind").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr in
                        ("debug", "info", "warning", "error", "exception", "critical")):
                    continue
                if not (isinstance(func.value, ast.Name) and func.value.id == "logger"):
                    continue
                for argument in node.args[1:]:
                    for child in ast.walk(argument):
                        if isinstance(child, ast.Name) and any(
                                word in child.id.lower() for word in banned):
                            offenders.append(f"{path.name}:{node.lineno}:{child.id}")
        self.assertEqual(offenders, [], f"log call may leak secrets: {offenders}")


class ClientIpTrustTests(ElenvindTestCase):
    @staticmethod
    def _scope(peer, forwarded=None, real_ip=None):
        headers = []
        if forwarded is not None:
            headers.append((b"x-forwarded-for", forwarded.encode()))
        if real_ip is not None:
            headers.append((b"x-real-ip", real_ip.encode()))
        return {"client": (peer, 5555), "headers": headers}

    def test_forwarded_for_is_used_only_for_trusted_peer(self):
        self._config["server"]["trusted_proxies"] = ["127.0.0.1", "::1"]
        self.assertEqual(get_client_ip(self._scope("127.0.0.1", "203.0.113.5")), "203.0.113.5")
        self.assertEqual(get_client_ip(self._scope("::1", "203.0.113.6")), "203.0.113.6")
        # 非可信直连：忽略伪造头
        self.assertEqual(get_client_ip(self._scope("198.51.100.7", "203.0.113.8")),
                         "198.51.100.7")

    def test_first_forwarded_address_wins_within_trusted_peer(self):
        self._config["server"]["trusted_proxies"] = ["127.0.0.1"]
        self.assertEqual(
            get_client_ip(self._scope("127.0.0.1", "203.0.113.9, 10.0.0.1, 10.0.0.2")),
            "203.0.113.9")

    def test_x_real_ip_is_not_trusted(self):
        """应用只采信 X-Forwarded-For；X-Real-IP 单独存在时不得改变客户端 IP。"""
        self._config["server"]["trusted_proxies"] = ["127.0.0.1"]
        self.assertEqual(get_client_ip(self._scope("127.0.0.1", None, "203.0.113.10")),
                         "127.0.0.1")
        # 同时存在时仍然取 X-Forwarded-For
        self.assertEqual(
            get_client_ip(self._scope("127.0.0.1", "203.0.113.11", "203.0.113.12")),
            "203.0.113.11")

    def test_missing_client_and_empty_forwarded_values(self):
        self._config["server"]["trusted_proxies"] = ["127.0.0.1"]
        self.assertEqual(get_client_ip({"headers": []}), "unknown")
        self.assertEqual(get_client_ip(self._scope("127.0.0.1", "   ")), "127.0.0.1")

    def test_client_ip_actually_used_for_rate_limiting(self):
        """可信代理下，限流按 X-Forwarded-For 的客户端 IP 计数（而不是代理 IP）。"""
        self.create_user(email="ip@example.com")
        self._config["login_limits"] = {
            "max_email_failures": 100, "email_window_seconds": 86400,
            "max_ip_failures": 2, "ip_window_seconds": 900,
            "max_global_failures": 1000, "global_window_seconds": 900,
        }
        csrf = self.fetch_csrf()

        def attempt(client_ip, email="ip@example.com"):
            body = urlencode({"csrf_token": csrf, "email": email,
                              "password": "wrong password"}).encode()
            return self.app.raw_request(
                "POST", "/login", b"",
                [("host", "example.com"),
                 ("content-type", "application/x-www-form-urlencoded"),
                 ("x-forwarded-for", client_ip),
                 ("cookie", f"csrf={csrf}"),
                 ("content-length", str(len(body)))],
                body=body)

        attempt("203.0.113.20")
        attempt("203.0.113.20")
        blocked = attempt("203.0.113.20")
        self.assertIn("Too many failed attempts from this address", blocked.text)

    def test_client_ip_is_counted_independently_of_the_proxy_address(self):
        """限流按 X-Forwarded-For 的客户端 IP 计数：不同客户端互不牵连。"""
        self.create_user(email="ip2@example.com")
        self._config["login_limits"] = {
            "max_email_failures": 100, "email_window_seconds": 86400,
            "max_ip_failures": 2, "ip_window_seconds": 900,
            "max_global_failures": 1000, "global_window_seconds": 900,
        }
        csrf = self.fetch_csrf()

        def attempt(client_ip):
            body = urlencode({"csrf_token": csrf, "email": "ip2@example.com",
                              "password": "wrong password"}).encode()
            return self.app.raw_request(
                "POST", "/login", b"",
                [("host", "example.com"),
                 ("content-type", "application/x-www-form-urlencoded"),
                 ("x-forwarded-for", client_ip),
                 ("cookie", f"csrf={csrf}"),
                 ("content-length", str(len(body)))],
                body=body)

        # 客户端 A 用满配额
        attempt("203.0.113.30")
        attempt("203.0.113.30")
        self.assertIn("Too many failed attempts from this address",
                      attempt("203.0.113.30").text)
        # 客户端 B 的计数独立：带正确的邮箱和密码应当可以登录
        self.create_user(email="ip3@example.com", password="correct horse battery")
        body = urlencode({"csrf_token": csrf, "email": "ip3@example.com",
                          "password": "correct horse battery"}).encode()
        login = self.app.raw_request(
            "POST", "/login", b"",
            [("host", "example.com"),
             ("content-type", "application/x-www-form-urlencoded"),
             ("x-forwarded-for", "203.0.113.31"),
             ("cookie", f"csrf={csrf}"),
             ("content-length", str(len(body)))],
            body=body)
        self.assertEqual(login.status, 302)

    def test_untrusted_peer_cannot_escape_ip_limit_with_forwarded_header(self):
        """非可信直连伪造 X-Forwarded-For 不能绕过 IP 限流。"""
        self.create_user(email="noip@example.com")
        self._config["login_limits"] = {
            "max_email_failures": 100, "email_window_seconds": 86400,
            "max_ip_failures": 2, "ip_window_seconds": 900,
            "max_global_failures": 1000, "global_window_seconds": 900,
        }
        self._config["server"]["trusted_proxies"] = []   # 谁都不信
        csrf = self.fetch_csrf()

        def attempt(fake_ip, peer):
            body = urlencode({"csrf_token": csrf, "email": "noip@example.com",
                              "password": "wrong password"}).encode()
            return self.app.raw_request(
                "POST", "/login", b"",
                [("host", "example.com"),
                 ("content-type", "application/x-www-form-urlencoded"),
                 ("x-forwarded-for", fake_ip),
                 ("cookie", f"csrf={csrf}"),
                 ("content-length", str(len(body)))],
                body=body, client=(peer, 4444))

        attempt("1.1.1.1", "198.51.100.1")
        attempt("2.2.2.2", "198.51.100.1")
        blocked = attempt("3.3.3.3", "198.51.100.1")
        self.assertIn("Too many failed attempts from this address", blocked.text)


if __name__ == "__main__":
    unittest.main()
