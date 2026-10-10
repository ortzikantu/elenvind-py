"""Core 契约：Markdown 净化、CSRF、回跳地址、静态资源、请求边界与安全头。

这些是 `core/templating.py` / `core/markdown.py` / `core/csrf.py` / `core/http.py`
注释里承诺过的行为；以前它们只有注释，没有守卫。
"""
from __future__ import annotations

import unittest
from html.parser import HTMLParser

from tests import support
from tests.support import ARTICLE_SLUG, request

DANGEROUS_TAGS = {"script", "iframe", "object", "embed", "form", "input", "style",
                  "svg", "math", "link", "meta", "base"}


class LiveHtml(HTMLParser):
    """收集"真的会生效"的危险构造（而不是被转义后显示成文本的那串字符）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in DANGEROUS_TAGS:
            self.problems.append(f"<{tag}>")
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name.startswith("on") or name in ("style", "srcdoc"):
                self.problems.append(f"{tag}[{name}]")
            if name in ("href", "src", "poster") and value.lower().startswith(
                    ("javascript:", "data:", "vbscript:")):
                self.problems.append(f"{tag}[{name}={value[:24]}]")
            if value.startswith("//"):
                self.problems.append(f"{tag}[{name}=protocol-relative]")

    handle_startendtag = handle_starttag


class MarkdownSanitizer(unittest.TestCase):
    def setUp(self):
        support.ensure_application()

    def _render(self, source: str) -> str:
        from elenvind.core.markdown import render_markdown
        return str(render_markdown(source))

    def test_dangerous_payloads_never_survive(self):
        payloads = [
            "<script>alert(1)</script>",
            '<img src=x onerror="alert(1)">',
            "[x](javascript:alert(1))",
            "![i](javascript:alert(1))",
            "[d](data:text/html;base64,PHNjcmlwdD4=)",
            "<iframe src=//evil.test></iframe>",
            "<svg/onload=alert(1)>",
            '<form action="//evil.test"><input name=x></form>',
            "<style>body{background:url(//evil.test)}</style>",
        ]
        parser = LiveHtml()
        parser.feed(self._render("\n\n".join(payloads)))
        self.assertEqual(parser.problems, [], f"live HTML survived: {parser.problems}")

    def test_video_directive_rejects_dangerous_schemes(self):
        rendered = self._render("@video(javascript:alert(1))")
        self.assertNotIn("<video", rendered)
        parser = LiveHtml()
        parser.feed(rendered)
        self.assertEqual(parser.problems, [])

    def test_inline_code_is_escaped_exactly_once(self):
        """`` `<div>` `` 应当渲染成可见的 `<div>`，而不是 `&lt;div&gt;` 字面量。"""
        rendered = self._render("inline: `<div>` end")
        self.assertIn("<code>&lt;div&gt;</code>", rendered)
        self.assertNotIn("&amp;lt;", rendered)

    def test_fenced_code_is_not_double_escaped(self):
        rendered = self._render("```html\n<script>x</script>\n```")
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("&amp;lt;", rendered)

    def test_plain_angle_brackets_are_preserved(self):
        rendered = self._render("a < b and c > d")
        self.assertIn("a &lt; b", rendered)


class NextPath(unittest.TestCase):
    """回跳地址：全项目唯一实现，必须拒绝跨站与所有可被浏览器归一化的形态。"""

    def setUp(self):
        from elenvind.core.http import safe_next_path
        self.safe = safe_next_path

    def test_rejects_cross_site_and_control_characters(self):
        # 注意：`/%09/…` 这种**未解码**写法是合法的站内路径片段，不该被拒
        # （查询串解码发生在此之前：`next=/%09/x` 到 safe_next_path 时仍是字面量
        #  `/%09/x`，而 `next=/\t/x` 解码后已是真 TAB，必须被拒）。
        for raw in ("//evil.test/x", "/\\evil.test", "/\t/evil.test",
                    "https://evil.test", "javascript:alert(1)", "evil.test",
                    "/a\r\nSet-Cookie: x=1", "", None, 123):
            self.assertEqual(self.safe(raw, default="/"), "/",
                             f"{raw!r} must not be accepted as a redirect target")

    def test_accepts_internal_paths(self):
        for raw in ("/", "/about", "/article/x?y=1#z", "/user"):
            self.assertEqual(self.safe(raw, default="/"), raw)


class StaticAssets(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        from elenvind.core.assets import resolve_asset
        self.resolve = resolve_asset

    def test_accepts_real_asset(self):
        resolved = self.resolve("css/style.css")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.is_file())

    def test_rejects_traversal_and_hidden_paths(self):
        for relative in ("../config.toml", "..%2fconfig.toml", "css/../../config.toml",
                         ".git/config", ".env", "", "..", "css/./style.css/.."):
            self.assertIsNone(self.resolve(relative),
                              f"{relative!r} must not resolve to a file")


class Csrf(unittest.TestCase):
    def test_token_format_and_comparison(self):
        from elenvind.core.csrf import is_valid_token, tokens_match
        from elenvind.core.security import generate_csrf_token

        token = generate_csrf_token()
        self.assertTrue(is_valid_token(token))
        self.assertTrue(tokens_match(token, token))
        self.assertFalse(tokens_match(token + "\n", token))     # fullmatch，不容忍换行
        self.assertFalse(tokens_match("short", token))
        self.assertFalse(tokens_match(None, token))

    def test_protected_methods(self):
        from elenvind.core.csrf import requires_protection
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertTrue(requires_protection(method))
        for method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            self.assertFalse(requires_protection(method))


class RequestContract(unittest.TestCase):
    """请求边界：方法白名单、framing、Content-Type、体积上限、安全头。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_security_headers_on_success_and_error(self):
        for path in ("/", "/no-such-page", "/login"):
            result = request("GET", path)
            self.assertEqual(result.header("x-content-type-options"), "nosniff")
            self.assertEqual(result.header("x-frame-options"), "DENY")
            self.assertIn("script-src 'none'", result.header("content-security-policy", ""))

    def test_head_has_no_body_but_keeps_length(self):
        result = request("HEAD", "/")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"")
        self.assertGreater(int(result.header("content-length", "0")), 0)

    def test_form_method_without_content_length_is_411(self):
        self.assertEqual(request("POST", "/login", content_length=False).status, 411)

    def test_delete_without_content_length_is_405_with_allow(self):
        """方法不允许优先于 411 —— 客户端要能读到 Allow 才能自愈。"""
        result = request("DELETE", "/", content_length=False)
        self.assertEqual(result.status, 405)
        self.assertIn("GET", result.header("allow", ""))

    def test_options_is_405_with_allow(self):
        result = request("OPTIONS", "/")
        self.assertEqual(result.status, 405)
        self.assertIn("GET", result.header("allow", ""))

    def test_unsupported_method_is_405(self):
        self.assertEqual(request("CONNECT", "/").status, 405)

    def test_unsupported_content_type_is_415(self):
        result = request("POST", "/login", body=b'{"a":1}',
                         headers={"Content-Type": "application/json"})
        self.assertEqual(result.status, 415)

    def test_oversized_body_is_413(self):
        # 测试配置的 max_body_size = 8192
        result = request("POST", "/login", body=b"x" * 9000,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(result.status, 413)

    def test_truncated_body_is_400(self):
        """声明 10 字节但只给 4 字节：绝不能把"少一点"当成合法表单。"""
        result = request("POST", "/login", body=b"abcd", declared_length=10,
                         headers={"Content-Type":
                                  "application/x-www-form-urlencoded"})
        self.assertEqual(result.status, 400)

    def test_form_post_without_csrf_is_400_not_500(self):
        result = request("POST", "/login", form={"email": "a@b.c"})
        self.assertEqual(result.status, 400)

    def test_login_page_is_not_stored_by_caches(self):
        result = request("GET", "/login")
        self.assertEqual(result.header("cache-control"), "no-store")
        self.assertTrue(result.cookies.get("csrf"))

    def test_static_asset_etag_and_304(self):
        first = request("GET", "/css/style.css")
        self.assertEqual(first.status, 200)
        etag = first.header("etag")
        self.assertTrue(etag)
        again = request("GET", "/css/style.css", headers={"If-None-Match": etag})
        self.assertEqual(again.status, 304)
        self.assertEqual(again.body, b"")

    def test_article_page_and_404(self):
        self.assertEqual(request("GET", f"/article/{ARTICLE_SLUG}").status, 200)
        self.assertEqual(request("GET", "/article/no-such-slug").status, 404)
        self.assertEqual(request("GET", "/no-such-page").status, 404)

    def test_custom_page_fallback(self):
        result = request("GET", "/about")
        self.assertEqual(result.status, 200)
        self.assertIn("custom page", result.text())


if __name__ == "__main__":
    unittest.main()
