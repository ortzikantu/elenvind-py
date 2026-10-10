"""日志卫生与代理信任。

对应 `core/app.py` 里"访问日志**不记**查询串 / 请求体 / Cookie / 凭据"的承诺
（注释说这些由 `tests/test_logging_proxy.py` 断言），以及 `core/utils.py` 里
"只有受信对端才能决定客户端 IP 与 scheme"的安全边界。
"""
from __future__ import annotations

import logging
import unittest

from tests import support
from tests.support import ARTICLE_SLUG


class LogHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def setUp(self):
        support.reset_database()

    def _flush(self) -> None:
        for handler in logging.getLogger().handlers:
            handler.flush()

    def test_credentials_and_query_strings_never_reach_the_log(self):
        password = "correct-horse-1"
        session = support.Session()
        session.create_account("Logger", "logger@example.test", password)
        token = session.csrf("/login")
        session.get("/?page=2&token=secret")          # 查询串不得进日志
        session.comment(ARTICLE_SLUG, "hello")

        self._flush()
        text = support.WORKSPACE.log.read_text(encoding="utf-8")
        self.assertIn("Request handled", text, "the access log should be written")
        self.assertNotIn(password, text)
        self.assertNotIn(token, text)
        self.assertNotIn("page=2", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("csrf_token", text)

    def test_access_log_fields_are_escaped_and_truncated(self):
        from elenvind.core.app import _log_field

        self.assertEqual(_log_field("/a\nFAKE"), "/a\\x0aFAKE")
        self.assertEqual(_log_field("/x\ty"), "/x\\x09y")
        self.assertEqual(_log_field("/caf\u00e9"), "/caf\\xe9")
        self.assertTrue(_log_field("A" * 500).endswith("…")
                        and len(_log_field("A" * 500)) <= 201)


class ProxyTrust(unittest.TestCase):
    """X-Forwarded-* 只在直连对端是受信代理时才被采信，且取**最右**有效跳。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_untrusted_peer_cannot_spoof_the_client_ip(self):
        from elenvind.core.utils import get_client_ip

        self.assertEqual(get_client_ip({}, "8.8.8.8"), "8.8.8.8")
        self.assertEqual(get_client_ip({"x-forwarded-for": "1.2.3.4"}, "8.8.8.8"), "8.8.8.8")
        self.assertEqual(get_client_ip({"x-forwarded-for": "not-an-ip"}, "8.8.8.8"), "8.8.8.8")

    def test_trusted_proxy_takes_the_rightmost_untrusted_hop(self):
        from elenvind.core.utils import get_client_ip

        # 覆盖式：只剩真实客户端
        self.assertEqual(get_client_ip({"x-forwarded-for": "203.0.113.9"}, "127.0.0.1"),
                         "203.0.113.9")
        # 追加式：左侧是客户端自己塞的，必须取右侧那一跳
        self.assertEqual(
            get_client_ip({"x-forwarded-for": "1.2.3.4, 203.0.113.9"}, "127.0.0.1"),
            "203.0.113.9")
        self.assertEqual(
            get_client_ip({"x-forwarded-for": "9.9.9.9, 127.0.0.1"}, "127.0.0.1"),
            "9.9.9.9")

    def test_scheme_only_from_a_trusted_proxy(self):
        from elenvind.core.utils import get_request_scheme

        http_from_trusted = {"wsgi.url_scheme": "http", "REMOTE_ADDR": "127.0.0.1"}
        http_from_remote = {"wsgi.url_scheme": "http", "REMOTE_ADDR": "8.8.8.8"}
        self.assertEqual(
            get_request_scheme(http_from_trusted, {"x-forwarded-proto": "https"}), "https")
        self.assertEqual(
            get_request_scheme(http_from_remote, {"x-forwarded-proto": "https"}), "http")
        self.assertEqual(
            get_request_scheme(http_from_trusted, {"x-forwarded-proto": "gopher"}), "http")

    def test_https_requests_get_hsts_and_secure_cookies(self):
        result = support.request("GET", "/login", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(result.header("strict-transport-security"), "max-age=300")
        self.assertTrue(any("Secure" in value for value in result.all_headers("set-cookie")))

    def test_http_requests_do_not_get_hsts(self):
        result = support.request("GET", "/login")
        self.assertIsNone(result.header("strict-transport-security"))
        self.assertFalse(any("Secure" in value for value in result.all_headers("set-cookie")))

    def test_host_header_cannot_poison_absolute_urls(self):
        result = support.request("GET", "/sitemap.xml", headers={"Host": "evil.test"})
        self.assertNotIn("evil.test", result.text())
        self.assertIn("https://test.invalid", result.text())


if __name__ == "__main__":
    unittest.main()
