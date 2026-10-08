"""安全回归测试（端到端）：存储型/反射型 XSS、CRLF 注入、缓存泄漏、错误处理。

XSS 断言用标准库 HTMLParser 解析真实响应，检查"是否真的产生了可执行标签/事件属性"，
而不是做字符串包含匹配——转义后的文本（如 &lt;img onerror=...&gt;）是安全的，
字符串匹配会误报。
"""
import unittest
from html.parser import HTMLParser

from tests.support import ElenvindTestCase

XSS_PAYLOADS = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    "javascript:alert(1)",
    '"><script>alert(1)</script>',
    "'><img src=x onerror=alert(1)>",
    "<iframe src=javascript:alert(1)></iframe>",
    "<body onload=alert(1)>",
    "{{7*7}}",
    "${alert(1)}",
    "%3Cscript%3Ealert(1)%3C/script%3E",
    "\\<script\\>alert(1)\\<\\/script\\>",
)

# 一旦由用户输入产生就是 XSS 的标签（模板自己不会输出这些）
FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "base", "style", "math",
                  "applet", "frame", "frameset", "noscript", "template", "marquee"}

# 安全属性白名单之外的事件属性
EVENT_ATTR_PREFIX = "on"


class _HtmlAudit(HTMLParser):
    """收集文档里出现的标签、事件属性与 URL 属性。"""

    URL_ATTRS = {"href", "src", "action", "poster", "data", "formaction", "srcset"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.event_attrs = []
        self.forbidden = []
        self.url_attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag in FORBIDDEN_TAGS:
            self.forbidden.append(tag)
        for name, value in attrs:
            lowered = name.lower()
            if lowered.startswith(EVENT_ATTR_PREFIX):
                self.event_attrs.append((tag, name, value))
            if lowered in self.URL_ATTRS and value:
                self.url_attrs.append((tag, lowered, value))

    handle_startendtag = handle_starttag


def audit_html(text):
    parser = _HtmlAudit()
    parser.feed(text)
    parser.close()
    return parser


class StoredXssTests(ElenvindTestCase):
    """评论内容、昵称、文章元数据都是用户/运营可控输入，输出必须安全。"""

    def setUp(self):
        super().setUp()
        self.write_article("post", "article body", {"title": "Post", "date": "2026-01-01"})

    def _assert_no_injection(self, html, payload):
        audit = audit_html(html)
        self.assertEqual(audit.event_attrs, [],
                         f"payload {payload!r} produced event attributes {audit.event_attrs}")
        self.assertEqual(audit.forbidden, [],
                         f"payload {payload!r} produced forbidden tags {audit.forbidden}")
        # 危险协议不得进入任何 URL 属性
        for tag, name, value in audit.url_attrs:
            lowered = value.strip().lower()
            self.assertFalse(
                lowered.startswith(("javascript:", "vbscript:", "data:text/html")),
                f"payload {payload!r} produced dangerous {name} on <{tag}>: {value!r}")
        # 含 HTML 元字符的载荷必须被转义，不得原样出现
        if any(char in payload for char in '<>"\''):
            self.assertNotIn(payload, html,
                             f"payload {payload!r} was reflected verbatim")

    def test_comment_payloads_are_escaped(self):
        from elenvind.db.comment import create_comment

        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        for payload in XSS_PAYLOADS:
            with self.subTest(payload=payload):
                create_comment("post", user_id, payload)
        response = self.app.request("GET", "/article/post")
        self.assertEqual(response.status, 200)
        for payload in XSS_PAYLOADS:
            self._assert_no_injection(response.text, payload)

    def test_nickname_payloads_are_escaped_everywhere(self):
        for payload in XSS_PAYLOADS:
            with self.subTest(payload=payload):
                user_id = self._make_user(payload)
                session, csrf = self.login_ok(f"user{user_id}@example.com", "password-123")
                for path in ("/", "/article/post", "/user"):
                    response = self.app.request("GET", path,
                                                cookies=self.app_cookies(session=session,
                                                                         csrf=csrf))
                    self.assertEqual(response.status, 200)
                    self._assert_no_injection(response.text, payload)

    def test_article_title_payload_is_escaped(self):
        for index, payload in enumerate(XSS_PAYLOADS):
            with self.subTest(payload=payload):
                slug = f"xss{index}"
                self.write_article(slug, "body", {"title": payload, "date": "2026-01-01"})
                response = self.app.request("GET", f"/article/{slug}")
                self._assert_no_injection(response.text, payload)

    def test_custom_page_body_payload_is_not_raw_html(self):
        for payload in XSS_PAYLOADS:
            with self.subTest(payload=payload):
                self.write_page("hostile", payload)
                response = self.app.request("GET", "/hostile")
                self.assertEqual(response.status, 200)
                self._assert_no_injection(response.text, payload)

    def test_user_page_reflects_escaped_values(self):
        payload = '"><script>alert(1)</script>'
        user_id = self._make_user(payload)
        session, csrf = self.login_ok(f"user{user_id}@example.com", "password-123")
        response = self.app.request("GET", "/user",
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self._assert_no_injection(response.text, payload)

    def _make_user(self, nickname):
        from elenvind.db.user import create_user
        from elenvind.core.security import hash_password
        existing = len(self._all_emails())
        email = f"user{existing + 1}@example.com"
        return create_user(nickname, email, hash_password("password-123"))

    def _all_emails(self):
        from elenvind.db import connect
        with connect() as conn:
            return [row["email"] for row in conn.execute("SELECT email FROM user")]


class ReflectedXssTests(ElenvindTestCase):
    def test_query_string_is_not_reflected_raw(self):
        payloads = ('"><script>alert(1)</script>', "<script>alert(1)</script>",
                    "'><img src=x onerror=alert(1)>")
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.app.request("GET", "/", query={"page": payload,
                                                               "reply_to": payload,
                                                               "x": payload})
                self.assertEqual(response.status, 200)
                audit = audit_html(response.text)
                self.assertEqual(audit.event_attrs, [])
                self.assertEqual(audit.forbidden, [])

    def test_theme_next_is_not_reflected(self):
        payload = '"><script>alert(1)</script>'
        response = self.app.request("GET", "/theme",
                                    query={"mode": "dark", "next": payload})
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/")
        self.assertNotIn("<script", response.text)

    def test_unknown_path_with_payload_returns_404(self):
        response = self.app.request("GET", '/<script>alert(1)</script>')
        self.assertEqual(response.status, 404)
        self.assertNotIn("<script>alert(1)</script>", response.text)

    def test_error_pages_escape_messages(self):
        """评论被拒时渲染的错误提示必须转义（不能把输入当 HTML 输出）。

        走真实渲染路径：`build_comment_rows` 会转义评论正文，
        系统错误页渲染 `error_message` 时由 Jinja 自动转义。
        """
        from elenvind.db.comment import create_comment
        from elenvind.core.templating import render_template
        from elenvind.modules.blog import logic as blog

        user_id, _ = self.create_user()
        self.write_article("post", "body")
        create_comment("post", user_id, '<script>alert(1)</script>')
        rows, _total, _max = blog.build_comment_rows("post", None, max_length=1000)
        self.assertNotIn("<script>alert(1)</script>", str(rows[0]["content"]))
        self.assertIn("&lt;script&gt;", str(rows[0]["content"]))

        # 错误页文案同样必须被转义
        page = render_template("errors/error.html", {
            "error_title": "Bad Request",
            "error_message": '<script>alert(1)</script>',
        })
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


class CrlfInjectionTests(ElenvindTestCase):
    def test_cookie_values_with_control_characters_are_rejected(self):
        """值含控制字符时 http.cookies 直接拒绝，从根上杜绝响应头注入。"""
        from http.cookies import CookieError

        from elenvind.core.security import csrf_cookie_header, set_cookie_header

        for value in ("abc\r\nX-Injected: 1", "abc\ndef", "abc\rdef", "a\x00b"):
            with self.subTest(value=value):
                for builder in (set_cookie_header, csrf_cookie_header):
                    with self.assertRaises(CookieError):
                        builder(value)

    def test_suspicious_values_are_quoted_not_split(self):
        from elenvind.core.security import set_cookie_header

        header = set_cookie_header("abc; Path=/evil")
        self.assertNotIn(b"\r", header[1])
        self.assertNotIn(b"\n", header[1])
        # 分号被值引用/转义，不会在响应头里变成新的属性分隔
        self.assertTrue(header[1].startswith(b'session="abc'), header[1])
        self.assertEqual(header[1].count(b"; Path=/;"), 1)   # 只有模板自带的 Path 属性

    def test_redirect_location_has_no_crlf(self):
        for value in ("/x\r\nX-Injected: 1", "/x\ny", "//evil\r\n"):
            with self.subTest(value=value):
                response = self.app.request("GET", "/theme",
                                            query={"mode": "dark", "next": value})
                location = response.header("location")
                self.assertNotIn("\r", location)
                self.assertNotIn("\n", location)

    def test_cookie_header_parsing_ignores_malformed_pairs(self):
        """一个畸形片段不该让整条 Cookie 头失效（同名取最后一个）。

        走 WSGI 的真实入口：`HTTP_COOKIE` 是**一个**字符串，解析的唯一实现是
        `core.security.parse_cookie_header`（被 `http.collect_headers` 调用）。
        """
        from elenvind.core.security import parse_cookie_header

        raw = "session=abc; =broken; csrf=xyz; session=def"
        cookies = parse_cookie_header(raw)
        self.assertEqual(cookies.get("session"), "def")   # 同名取最后一个
        self.assertEqual(cookies.get("csrf"), "xyz")

    def test_malformed_cookie_does_not_log_everyone_out(self):
        """端到端：畸形 Cookie 片段 + 合法会话 Cookie 仍然能认出会话。

        回归背景：`http.cookies.SimpleCookie` 遇到 `=broken` 会把整条头丢掉，
        于是"一个损坏的无关 Cookie 让所有人掉线"。
        """
        user_id, password = self.create_user(email="cookie@example.com")
        session, _csrf = self.login_ok("cookie@example.com", password)
        response = self.app.request(
            "GET", "/user",
            extra_headers=[("cookie", f"=broken; session={session}")])
        self.assertEqual(response.status, 200)
        self.assertIn("cookie@example.com", response.text)

    def test_cookie_header_is_a_single_wsgi_value(self):
        """WSGI 只有 `HTTP_COOKIE` 一个键：服务器负责合并重复的 Cookie 头。

        旧实现自己遍历请求头列表并"逐个合并"，那是协议适配层的职责；
        现在应用只消费 `collect_headers()` 给出的那一个值（gunicorn 会把
        重复头用 `,` 连起来），因此这里断言的是这条唯一契约。
        """
        from elenvind.core.http import collect_headers

        headers = collect_headers({
            "HTTP_COOKIE": "session=abc, csrf=xyz",
            "HTTP_HOST": "example.com",
        })
        self.assertEqual(headers["cookie"], "session=abc, csrf=xyz")


class CacheLeakTests(ElenvindTestCase):
    def test_logged_in_pages_are_not_cacheable(self):
        self.create_user(email="cache@example.com")
        session, csrf = self.login_ok("cache@example.com", "correct horse battery")
        for path in ("/", "/user", "/login"):
            with self.subTest(path=path):
                response = self.app.request("GET", path,
                                            cookies=self.app_cookies(session=session, csrf=csrf))
                self.assertEqual(response.header("cache-control"), "no-store")

    def test_public_metadata_is_cacheable_but_generic(self):
        self.create_user(email="cache2@example.com")
        session, _ = self.login_ok("cache2@example.com", "correct horse battery")
        response = self.app.request("GET", "/robots.txt",
                                    cookies=self.app_cookies(session=session))
        self.assertEqual(response.header("cache-control"), "public, max-age=3600")
        self.assertNotIn("cache2@example.com", response.text)


class ServerErrorPageTests(ElenvindTestCase):
    """500 必须走自定义错误页，且**绝不**泄露 traceback / 异常类型。

    三层兜底都要拦住：
    1. 模块提供 server_error -> 带布局的错误页；
    2. 没提供 / 渲染失败 -> 纯文本 "Internal Server Error"；
    3. 请求对象都没有（畸形请求）-> 纯文本。
    """

    #: 任何一层都不允许出现在响应体里的痕迹
    NEVER_LEAK = ("Traceback", "traceback", "RuntimeError", "ValueError",
                  "File \"", 'File "', "internal detail", "/etc/passwd",
                  "SECRET-MARKER", "SiteError")

    def _boom_response(self, message="internal detail: /etc/passwd"):
        """把首页 handler 换成一个必抛异常的版本，拿到 500 响应。"""
        from elenvind.app import app

        target = next(route for route in app.router.routes if route.path == "/")
        original = target.handler

        def boom(request):
            raise RuntimeError(message)

        target.handler = boom
        try:
            with self.assertLogs("elenvind.core.app", level="ERROR") as captured:
                response = self.app.request("GET", "/")
        finally:
            target.handler = original
        return response, captured

    def test_500_uses_the_html_error_template(self):
        response, _ = self._boom_response()
        self.assertEqual(response.status, 500)
        self.assertTrue(response.content_type.startswith("text/html"),
                        response.content_type)
        self.assertIn("<!DOCTYPE html>", response.text)
        self.assertIn("500 Internal Server Error", response.text)
        # 带布局：站点页头/页脚都在（与 404/403 观感一致）
        self.assertIn("<header>", response.text)
        self.assertIn("<footer>", response.text)

    def test_500_never_leaks_exception_details(self):
        response, _ = self._boom_response()
        for leak in self.NEVER_LEAK:
            with self.subTest(leak=leak):
                self.assertNotIn(leak, response.text)

    def test_traceback_goes_to_the_log_instead(self):
        _, captured = self._boom_response()
        logged = "\n".join(captured.output)
        self.assertIn("Traceback", logged)          # 堆栈确实被记录了
        self.assertIn("RuntimeError", logged)
        self.assertIn("/etc/passwd", logged)

    def test_500_response_carries_security_headers(self):
        """错误响应也走统一的响应收尾（安全头 + 缓存策略）。"""
        response, _ = self._boom_response()
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertEqual(response.header("referrer-policy"),
                         "strict-origin-when-cross-origin")
        self.assertIsNotNone(response.header("content-security-policy"))
        self.assertEqual(response.header("cache-control"), "no-store")

    def test_falls_back_to_plain_text_without_handler(self):
        """没提供 server_error 时回落纯文本，且同样不泄露。"""
        from elenvind.core.app import App

        mini = App()

        def boom(request):
            raise ValueError("layer2-marker")

        mini.router.route("/boom", methods=["GET"])(boom)
        status, body, content_type = self._drive(mini, "/boom")
        self.assertEqual(status, 500)
        self.assertTrue(content_type.startswith("text/plain"), content_type)
        self.assertEqual(body, b"Internal Server Error")
        self.assertNotIn(b"layer2-marker", body)

    def test_falls_back_when_error_page_rendering_itself_fails(self):
        """错误页渲染本身抛异常时也必须给出 500，而不是二级异常。"""
        from elenvind.core.app import App

        mini = App()

        def bad_renderer(request):
            raise RuntimeError("layer3-render-failed")

        mini.server_error = bad_renderer

        def boom(request):
            raise ValueError("original-failure")

        mini.router.route("/boom", methods=["GET"])(boom)
        status, body, content_type = self._drive(mini, "/boom")
        self.assertEqual(status, 500)
        self.assertTrue(content_type.startswith("text/plain"), content_type)
        self.assertNotIn(b"layer3-render-failed", body)
        self.assertNotIn(b"original-failure", body)

    def test_malformed_request_error_page_is_plain_text(self):
        """请求对象还没构造出来时（畸形 Content-Length）也是纯文本 500/400。"""
        response = self.app.raw_request(
            "POST", "/login", b"",
            [("host", "example.com"), ("content-length", "not-a-number")], body=b"")
        self.assertEqual(response.status, 400)
        self.assertTrue(response.content_type.startswith("text/plain"),
                        response.content_type)

    def test_error_template_renders_no_dynamic_exception_data(self):
        """模板本身只输出固定文案：结构上就不存在泄漏通道。"""
        import pathlib
        from tests.support import PROJECT_ROOT
        template = (PROJECT_ROOT / "elenvind" / "templates" / "errors"
                    / "error.html").read_text(encoding="utf-8")
        # 只允许固定文案变量，不允许任何"异常/堆栈/路径"类变量
        for forbidden in ("traceback", "exception", "exc_info", "error_detail",
                          "error_debug", "stack", "repr(", "args"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template.lower())
        self.assertIn("error_title", template)
        self.assertIn("error_message", template)

    def test_server_error_handler_is_wired(self):
        """回归：system 模块提供的 500 渲染函数必须真的接到 App 上。

        曾经它被定义但从未装配 —— 500 一直走裸文本。
        （装配点从旧的 `registry` 收敛到唯一的组合入口 `elenvind/app.py`。）
        """
        from elenvind.app import app
        from elenvind.modules.system import routes as system_routes
        self.assertIsNotNone(app.server_error)
        self.assertIs(app.server_error, system_routes.server_error)

    @staticmethod
    def _drive(mini, path):
        from tests.support import build_environ, call_wsgi
        import logging

        logging.disable(logging.CRITICAL)
        try:
            response = call_wsgi(mini, build_environ("GET", path))
        finally:
            logging.disable(logging.NOTSET)
        return response.status, response.body, response.content_type


class ErrorHandlingTests(ElenvindTestCase):
    def test_unexpected_exception_becomes_500_without_leaking_details(self):
        from elenvind.app import app

        target = next(route for route in app.router.routes if route.path == "/")
        original = target.handler

        def boom(request):
            raise RuntimeError("internal detail: /etc/passwd")

        target.handler = boom
        try:
            with self.assertLogs("elenvind.core.app", level="ERROR") as captured:
                response = self.app.request("GET", "/")
        finally:
            target.handler = original

        self.assertEqual(response.status, 500)
        # 现在 500 走带布局的错误页（与 404/403 一致），不再是裸文本
        self.assertIn("<!DOCTYPE html>", response.text)
        self.assertIn("500", response.text)

        # 关键：页面上不得出现任何异常细节
        for leak in ("/etc/passwd", "RuntimeError", "Traceback", "traceback",
                     "internal detail", 'File "', "line "):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, response.text)

        # traceback 必须进了日志（而不是被丢弃）
        logged = "\n".join(captured.output)
        self.assertIn("RuntimeError", logged)
        self.assertIn("/etc/passwd", logged)
        self.assertIn("Traceback", logged)

    def test_404_page_is_rendered_html(self):
        response = self.app.request("GET", "/nope")
        self.assertEqual(response.status, 404)
        self.assertIn("<!DOCTYPE html>", response.text)

    def test_response_headers_are_not_duplicated(self):
        response = self.app.request("GET", "/")
        names = [name for name, _ in response.headers]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
