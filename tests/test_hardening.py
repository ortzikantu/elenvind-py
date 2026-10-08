"""加固回归：CSP 默认值、非回环监听告警、静态隐藏文件（Phase 4-E/F/G）。"""

import io
import logging
import os
import unittest

from tests.support import ElenvindTestCase

from elenvind import app as app_module
from elenvind.core import assets
from elenvind.core.security import DEFAULT_CSP_DIRECTIVES


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [record.getMessage() for record in self.records]


class CspDefaultsTests(ElenvindTestCase):
    """CSP 的**产品取向**：外链图片/视频默认放行（减轻单机静态服务压力）。

    因此这里锁定的不是"禁止远程资源"，而是三条不会随取向变化的底线：
    1. 不用 `*` / `unsafe-eval` 这类"什么都允许"的来源；
    2. 脚本一律禁止（`script-src 'none'`：站点是 Zero-JS）；
    3. 想收紧必须写**显式主机**（由 tests/test_doc_consistency.py 守卫）。
    """

    def test_no_wildcard_or_unsafe_eval_sources(self):
        for name, values in DEFAULT_CSP_DIRECTIVES.items():
            with self.subTest(directive=name):
                joined = " ".join(values)
                self.assertNotIn("*", joined, f"{name} 不应出现 *：{joined}")
                self.assertNotIn("'unsafe-eval'", joined, f"{name} 不应允许 eval")

    def test_scripts_are_always_blocked(self):
        self.assertEqual(tuple(DEFAULT_CSP_DIRECTIVES["script-src"]), ("'none'",))

    def test_external_media_is_allowed_on_purpose(self):
        """外链是刻意允许的（文章配图/视频走外链，内置 static 只放图标/logo）。"""
        for name in ("img-src", "media-src"):
            with self.subTest(directive=name):
                self.assertIn("https:", DEFAULT_CSP_DIRECTIVES[name])

    def test_response_header_carries_the_defaults(self):
        response = self.app.request("GET", "/")
        header = {name.lower(): value for name, value in
                  ((name.decode("latin-1"), value.decode("latin-1"))
                   for name, value in response.headers)}["content-security-policy"]
        self.assertIn("script-src 'none'", header)
        self.assertNotIn("*", header)
        self.assertIn("frame-ancestors 'none'", header)

    def test_style_src_keeps_unsafe_inline_for_the_hero(self):
        """`style-src 'unsafe-inline'` 是 hero 内联背景图所必需（已文档化的取舍）。"""
        self.assertIn("'unsafe-inline'", DEFAULT_CSP_DIRECTIVES["style-src"])


class ExposedBindWarningTests(ElenvindTestCase):
    """监听非回环地址 + 信任转发头 => 必须给出显式告警。"""

    def _capture(self):
        handler = _LogCapture()
        logger = logging.getLogger("elenvind")
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(previous)))
        return handler

    def test_warns_when_public_bind_trusts_forwarded_headers(self):
        handler = self._capture()
        self._config["server"] = {"host": "0.0.0.0", "port": 6789,
                                  "trusted_proxies": ["127.0.0.1"]}
        app_module.warn_on_exposed_bind()
        messages = " | ".join(handler.messages())
        self.assertIn("bypass the reverse proxy", messages, messages)

    def test_silent_for_loopback_bind(self):
        handler = self._capture()
        self._config["server"] = {"host": "127.0.0.1", "port": 6789,
                                  "trusted_proxies": ["127.0.0.1"]}
        app_module.warn_on_exposed_bind()
        self.assertEqual(handler.messages(), [])

    def test_silent_when_no_forwarded_headers_are_trusted(self):
        handler = self._capture()
        self._config["server"] = {"host": "0.0.0.0", "port": 6789,
                                  "trusted_proxies": []}
        app_module.warn_on_exposed_bind()
        self.assertEqual(handler.messages(), [])


class StaticHiddenFileTests(ElenvindTestCase):
    """解析之后再查隐藏段：`static/x.png -> static/.secret` 不能被发出去。"""

    def setUp(self):
        super().setUp()
        self.root = self.tmpdir / "static-root"
        (self.root / "imgs").mkdir(parents=True)
        (self.root / ".secret").write_text("PRIVATE", encoding="utf-8")
        (self.root / "imgs" / "ok.png").write_bytes(b"PNG")
        self._original_static = assets.STATIC_DIR
        assets.STATIC_DIR = self.root
        self.addCleanup(lambda: setattr(assets, "STATIC_DIR", self._original_static))

    def test_symlink_to_hidden_file_is_rejected(self):
        link = self.root / "imgs" / "leak.png"
        try:
            os.symlink(self.root / ".secret", link)
        except (OSError, NotImplementedError):            # pragma: no cover
            self.skipTest("平台/权限不支持创建符号链接")
        self.assertIsNone(assets.resolve_asset("imgs/leak.png"))

    def test_hidden_url_segment_is_rejected(self):
        self.assertIsNone(assets.resolve_asset(".secret"))
        self.assertIsNone(assets.resolve_asset("imgs/../.secret"))

    def test_normal_asset_still_resolves(self):
        self.assertEqual(assets.resolve_asset("imgs/ok.png"),
                         (self.root / "imgs" / "ok.png").resolve())


class TokenAuditTests(ElenvindTestCase):
    """会话/CSRF 轮换与失效（Phase 4-H 的最小验证面）。"""

    def test_login_rotates_the_session_and_invalidates_the_old_one(self):
        self.create_user(email="rot@example.com")          # 默认口令见 support.create_user
        first_token, _ = self.login_ok("rot@example.com", "correct horse battery")
        second_token, _ = self.login_ok("rot@example.com", "correct horse battery")
        self.assertNotEqual(first_token, second_token)
        # 旧 token 已失效：用它访问受保护页面只会看到未登录状态
        stale = self.app.request("GET", "/user", cookies={self.session_cookie_name(): first_token})
        self.assertNotIn("rot@example.com", stale.text)

    def test_logout_only_kills_the_current_session(self):
        """登出只销毁当前会话，不影响其它账号的会话。

        （同一账号不会有多个会话：登录必须轮换并作废该账号的全部旧会话，
        这是防会话固定的要求 —— 见 test_login_rotates_...。）
        """
        self.create_user(email="a@example.com")
        self.create_user(email="b@example.com")
        a_token, a_csrf = self.login_ok("a@example.com", "correct horse battery")
        b_token, _ = self.login_ok("b@example.com", "correct horse battery")

        self.app.request("POST", "/logout", form={"csrf_token": a_csrf},
                         cookies=self.app_cookies(a_token, a_csrf))

        gone = self.app.request("GET", "/user", cookies={self.session_cookie_name(): a_token})
        self.assertNotIn("a@example.com", gone.text)
        alive = self.app.request("GET", "/user", cookies={self.session_cookie_name(): b_token})
        self.assertIn("b@example.com", alive.text)


if __name__ == "__main__":
    unittest.main()
