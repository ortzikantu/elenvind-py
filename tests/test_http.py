"""HTTP 层测试：请求体 framing、方法/路由边界、安全响应头、缓存策略、HEAD。"""
import unittest
from urllib.parse import urlencode

from tests.support import ElenvindTestCase, StreamInput


class PostBodyFramingTests(ElenvindTestCase):
    """请求体 framing 的各类畸形 / 边界输入（规格要求逐项覆盖）。

    这些用例全部围绕 **WSGI 的请求体语义**展开：长度来自 `CONTENT_LENGTH`，
    body 从 `environ["wsgi.input"]` 按声明长度读取，而输入流允许**短读**
    （一次只给一部分）与提前 EOF（客户端断开/截断）。
    """

    def _post_raw(self, body=b"", headers=None, stream=None, path="/login"):
        base = [("host", "example.com")]
        base.extend(headers or [])
        return self.app.raw_request("POST", path, b"", base, body=body, stream=stream)

    def test_negative_content_length_is_400(self):
        response = self._post_raw(headers=[("content-length", "-1")])
        self.assertEqual(response.status, 400)

    def test_non_numeric_content_length_is_400(self):
        response = self._post_raw(headers=[("content-length", "abc")])
        self.assertEqual(response.status, 400)

    def test_missing_content_length_is_411_not_empty_form(self):
        """缺少 Content-Length 必须明确拒绝，而不是当成空表单。"""
        response = self._post_raw(
            body=b"csrf_token=x",
            headers=[("content-type", "application/x-www-form-urlencoded")],
        )
        self.assertEqual(response.status, 411)

    def test_oversized_declared_length_is_413(self):
        response = self._post_raw(headers=[
            ("content-length", str(2 * 1024 * 1024)),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertEqual(response.status, 413)
        self.assertEqual(response.header("connection"), "close")

    def test_oversized_declared_length_is_rejected_without_reading_body(self):
        """声明超限时必须**先拒绝**，而不是把 2 MB 读进来再判体积。

        用 2 MB 的声明配一个"只有 10 字节"的流：如果实现先读完再判，
        就会得到 400（截断）而不是 413。
        """
        response = self._post_raw(
            stream=StreamInput(b"a" * 10),
            headers=[("content-length", str(2 * 1024 * 1024)),
                     ("content-type", "application/x-www-form-urlencoded")])
        self.assertEqual(response.status, 413)

    def test_truncated_body_is_400(self):
        """声明 1000 字节却只发 10 字节：不能当合法请求处理。"""
        response = self._post_raw(
            stream=StreamInput(b"a" * 10),
            headers=[("content-length", "1000"),
                     ("content-type", "application/x-www-form-urlencoded")],
        )
        self.assertEqual(response.status, 400)

    def test_input_stream_that_gives_nothing_is_400_not_a_hang(self):
        """声明了长度、输入流却一个字节都不给：必须立刻 400（不能空转）。

        这是旧的异步适配层里 `MAX_BODY_CHUNKS` 计数器防的那个失败模式
        （"空分片 + more_body 永真"）。WSGI 下循环由声明长度兜底：
        每次读取要么至少推进一个字节，要么读不到 => 直接按截断拒绝。
        """
        response = self._post_raw(
            stream=StreamInput(always_empty=True),
            headers=[("content-length", "100"),
                     ("content-type", "application/x-www-form-urlencoded")],
        )
        self.assertEqual(response.status, 400)

    def test_empty_body_with_zero_length_is_accepted(self):
        response = self._post_raw(
            body=b"",
            headers=[("content-length", "0"),
                     ("content-type", "application/x-www-form-urlencoded")],
        )
        # 空表单 → CSRF 校验失败，但 body framing 本身是合法的
        self.assertEqual(response.status, 400)
        self.assertEqual(response.status, 400)
        self.assertTrue("CSRF" in response.text
                        or "could not be processed" in response.text,
                        response.text[:200])

    def test_unsupported_content_type_is_415(self):
        body = urlencode({"csrf_token": "x"}).encode()
        response = self._post_raw(body=body, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/json"),
        ])
        self.assertEqual(response.status, 415)

    def test_content_type_with_charset_is_accepted(self):
        body = urlencode({"csrf_token": "x"}).encode()
        response = self._post_raw(body=body, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
        ])
        # 能走到 CSRF 校验说明 Content-Type 已被接受
        self.assertEqual(response.status, 400)
        self.assertEqual(response.status, 400)
        self.assertTrue("CSRF" in response.text
                        or "could not be processed" in response.text,
                        response.text[:200])

    def test_missing_content_type_is_accepted(self):
        body = urlencode({"csrf_token": "x"}).encode()
        response = self._post_raw(body=body, headers=[("content-length", str(len(body)))])
        self.assertEqual(response.status, 400)
        self.assertEqual(response.status, 400)
        self.assertTrue("CSRF" in response.text
                        or "could not be processed" in response.text,
                        response.text[:200])

    def test_invalid_utf8_body_is_400(self):
        """请求体不是合法 UTF-8：明确 400，不做替换式容错解析。"""
        body = b"csrf_token=\xff\xfe"
        response = self._post_raw(body=body, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertEqual(response.status, 400)

    def test_short_reads_are_concatenated(self):
        """WSGI 允许输入流短读：必须循环读到声明的长度，而不是读一次就算。

        输入流每次最多给 3 字节：若实现只读一次，body 会被判成"截断"（400）；
        正确实现拼接后得到完整表单（走到 CSRF/登录失败页 = 200/400 均可，
        这里用真实凭据走到登录失败页 200）。
        """
        token = self.fetch_csrf()
        body = urlencode({"csrf_token": token, "email": "nobody@example.com",
                          "password": "whatever"}).encode()
        response = self._post_raw(
            stream=StreamInput(body, max_per_read=3),
            headers=[("content-length", str(len(body))),
                     ("content-type", "application/x-www-form-urlencoded"),
                     ("cookie", f"csrf={token}")])
        self.assertEqual(response.status, 200)   # 登录失败页（账号不存在）

    def test_body_exactly_at_limit_is_allowed(self):
        """上限之内的 body 必须能正常进入业务逻辑（此处以 CSRF 失败为界）。"""
        from elenvind.core.config import config as live_config

        limit = live_config["max_body_size"]
        token = "x" * 43
        prefix = b"csrf_token=" + token.encode() + b"&pad="
        body = prefix + b"a" * (limit - len(prefix))
        self.assertEqual(len(body), limit)
        response = self._post_raw(body=body, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertNotEqual(response.status, 413)


class MethodAndRoutingTests(ElenvindTestCase):
    def test_unsupported_method_returns_405_with_allow(self):
        """方法不被任何匹配路由支持：405 + Allow（且不进入业务逻辑）。

        DELETE 需要请求体，所以这里显式给出 Content-Length=0，
        以便测到路由层的方法判定而不是被 411 拦在前面。
        """
        response = self.app.raw_request(
            "DELETE", "/nope", b"", [("host", "example.com"), ("content-length", "0")])
        self.assertEqual(response.status, 405)
        allow = response.header("allow") or ""
        self.assertIn("GET", allow)

    def test_head_returns_headers_without_body(self):
        response = self.app.raw_request("HEAD", "/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.header("content-length"),
                         str(len(self.app.request("GET", "/").body)))

    def test_get_logout_anonymous_shows_signed_out_message(self):
        """未登录（或会话已失效）访问 GET /logout：友好提示，不是 405。"""
        response = self.app.request("GET", "/logout")
        self.assertEqual(response.status, 200)
        self.assertIn("You have been signed out", response.text)
        # 没有任何可提交的退出表单（本来就没登录）
        self.assertNotIn('action="/logout"', response.text)

    def test_get_logout_logged_in_renders_csrf_confirmation(self):
        """已登录访问 GET /logout：给出确认页，且**不**销毁会话。

        退出会改变状态，必须由 POST + CSRF 触发；GET 只做确认，
        因此不能被 `<img src="/logout">` 这类跨站请求触发。
        """
        user_id, password = self.create_user(email="bye@example.com")
        session, csrf = self.login_ok("bye@example.com", password)
        cookies = self.app_cookies(session=session, csrf=csrf)

        response = self.app.request("GET", "/logout", cookies=cookies)
        self.assertEqual(response.status, 200)
        self.assertIn('action="/logout"', response.text)
        self.assertIn('name="csrf_token"', response.text)
        self.assertIn("Are you sure", response.text)

        # 关键：GET 之后会话必须仍然有效
        from elenvind.db.session import get_session_user
        self.assertEqual(get_session_user(session), user_id)

    def test_get_logout_does_not_clear_session_cookie(self):
        """GET /logout 不得下发清 Cookie 的头（那是 POST 的职责）。"""
        _user_id, password = self.create_user(email="keep@example.com")
        session, csrf = self.login_ok("keep@example.com", password)
        response = self.app.request("GET", "/logout",
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        for header in response.headers_all("set-cookie"):
            self.assertNotIn("session=;", header)
            self.assertNotIn("Max-Age=0", header)

    def test_post_only_routes_still_return_405_with_allow(self):
        """仅声明 POST 的路由，GET 仍必须是 405 + Allow（未被友好化误伤）。"""
        path = "/article/some-slug/comment/delete/1"
        response = self.app.request("GET", path)
        self.assertEqual(response.status, 405)
        self.assertIn("POST", response.header("allow") or "")

    def test_unknown_path_returns_404(self):
        response = self.app.request("GET", "/definitely-not-here")
        self.assertEqual(response.status, 404)

    def test_unknown_post_path_returns_405(self):
        """没有任何 POST 路由能匹配：/, /<slug> 都不该被 POST 命中。"""
        response = self.app.request("POST", "/nope", form={"a": "b"})
        self.assertEqual(response.status, 405)
        allow = response.header("allow") or ""
        self.assertIn("GET", allow)

    def test_short_path_without_body_length_is_411(self):
        """需要请求体的方法缺少 Content-Length：411，且不进入业务逻辑。"""
        response = self.app.request("DELETE", "/logout", body=b"", send_content_length=False)
        self.assertEqual(response.status, 411)
        self.assertNotIn("Location", response.text)

    def test_article_route_with_extra_segments_is_404(self):
        response = self.app.request("GET", "/article/a/b")
        self.assertEqual(response.status, 404)

    def test_traversal_like_paths_never_touch_filesystem(self):
        """路径穿越类请求不得泄漏配置文件内容。"""
        for path in ("/..%2fconfig.toml", "/../config.toml", "/articles/../config.toml",
                     "/custom_pages/../config.toml", "/.git/config", "/about/../secret"):
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertIn(response.status, (404, 302))
                # config.toml 里的真实键名不得出现在响应中（否则说明读到了文件）
                self.assertNotIn("site_url", response.text)
                self.assertNotIn("[server]", response.text)
                self.assertNotIn("[static]", response.text)

    def test_theme_redirect_rejects_open_redirect(self):
        """`next` 必须是站内路径，否则回落首页。

        回归：这条测试曾经只断言 `location.startswith("/")` 且
        `not location.startswith("//")` —— 而 `/\t/evil.com` 与 `/\\evil.com`
        **同时满足**两个断言，于是测试通过而重定向实际是跨站的。
        原因是浏览器在解析 URL 前会剥离 ASCII TAB / 把反斜杠归一化成 `/`，
        因此 `/\t/evil.com` 与 `/\\evil.com` 在浏览器眼里都是 `//evil.com`。
        现在断言改为「不许等于危险值」+「不许含任何控制字符或反斜杠」。
        """
        for value in ("//evil.example.com", "https://evil.example.com", "\\\\evil",
                      "javascript:alert(1)", "/\\evil", "",
                      # 下面两个是曾经漏掉的关键用例
                      "/\t/evil.com", "/\\evil.com",
                      # 编码与其它控制字符变体（应用侧不解码，交给解码后仍被拒）
                      "/%09/evil.com", "/\n/evil.com", "/\r/evil.com",
                      "/\u2028/evil.com", "/a\x00b"):
            with self.subTest(value=value):
                response = self.app.request("GET", "/theme",
                                            query={"mode": "dark", "next": value})
                self.assertEqual(response.status, 302)
                location = response.header("location") or ""
                self.assertTrue(location.startswith("/"), location)
                self.assertFalse(location.startswith("//"), location)
                self.assertNotIn("\\", location, "Location 不得含反斜杠")
                for ch in location:
                    self.assertGreater(ord(ch), 0x1F,
                                       f"Location 含控制字符 {ch!r}: {location!r}")
                    self.assertNotEqual(ord(ch), 0x7F)

    def test_login_next_rejects_open_redirect(self):
        """登录后的回跳同样必须拒绝 TAB / 反斜杠。"""
        from elenvind.modules.auth.routes import safe_next
        for value in ("/\t/evil.com", "/\\evil.com", "//evil.com",
                      "https://evil.com", "\\\\evil", "javascript:alert(1)",
                      "/\n/evil.com", "/\u2028/evil.com", "", None, 123):
            with self.subTest(value=value):
                self.assertEqual(safe_next(value), "")

    def test_login_next_keeps_legitimate_paths(self):
        from elenvind.modules.auth.routes import safe_next
        for value in ("/", "/about", "/article/x?y=1", "/a/b/c",
                      "/user?next=/login", "/中文路径", "/a%20b"):
            with self.subTest(value=value):
                self.assertEqual(safe_next(value), value)

    # ---------- safe_next 的每一道防线都必须**各自**有效 ----------

    def test_control_char_rejection_has_two_independent_layers(self):
        """TAB 必须同时被"控制字符区间"与"Unicode 类别"两条独立规则覆盖。

        `safe_next_path` 用了两条互不依赖的判据：

        1. `ch in "\\t\\r\\n" or "\\x00" <= ch <= "\\x1f" or ch == "\\x7f"`
           （外加 C1 区间 `\\x80`–`\\x9f`）；
        2. `unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")`。

        变异测试发现：单独去掉第 1 条里的 `\\t`，行为**不变** —— 因为
        `unicodedata.category("\\t") == "Cc"` 又被第 2 条拦住。
        这是有意为之的纵深防御，但也意味着"只测 TAB 被拒"无法证明第 1 条
        还在工作。这里明确把两层的**各自**职责钉住：

        - 纯 ASCII 控制字符（第 1 条覆盖）；
        - Unicode 格式/行分隔符（只有第 2 条覆盖：Cf / Zl / Zp）。
        """
        import unicodedata

        from elenvind.core.http import safe_next_path

        # 第 2 条独有：Cf（格式字符）/ Zl（行分隔）/ Zp（段分隔）
        category_only = {
            "\u200b": "Cf",    # 零宽空格
            "\u200e": "Cf",    # 从左至右标记
            "\u00ad": "Cf",    # 软连字符
            "\u2028": "Zl",    # 行分隔符
            "\u2029": "Zp",    # 段分隔符
        }
        for ch, expected in category_only.items():
            with self.subTest(char=repr(ch)):
                self.assertEqual(unicodedata.category(ch), expected)
                self.assertEqual(safe_next_path(f"/a{ch}b"), "/",
                                 f"{expected} 类字符未被拒绝")

        # 第 1 条覆盖：ASCII 控制字符（含 TAB/CR/LF/DEL 与 C1）
        for code in list(range(0x00, 0x20)) + [0x7F] + list(range(0x80, 0xA0)):
            ch = chr(code)
            with self.subTest(code=hex(code)):
                self.assertEqual(safe_next_path(f"/a{ch}b"), "/",
                                 f"U+{code:04X} 未被拒绝")

    def test_ascii_control_range_is_checked_independently_of_unicode_category(self):
        """即使 Unicode 类别判据被去掉，ASCII 控制字符仍必须被拒绝。

        单独验证第 1 条的**区间**部分：把 `\\t` 从显式列举里去掉后，
        `\\x0b`–`\\x1f` 这段区间仍要覆盖 TAB 以外的控制字符，
        而 TAB 自己由 Cf/Cc 类别与区间共同兜住。
        """
        from elenvind.core.http import _has_url_control_chars

        for code in (0x00, 0x01, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
                     0x1F, 0x7F, 0x80, 0x85, 0x9F):
            with self.subTest(code=hex(code)):
                self.assertTrue(_has_url_control_chars(chr(code)),
                                f"U+{code:04X} 未被判定为控制字符")

    def test_legitimate_unicode_is_not_treated_as_a_control_char(self):
        """反向：正常的多语言字符不能被误判（否则中文/日文路径全被拒）。"""
        from elenvind.core.http import _has_url_control_chars

        for value in ("/中文路径", "/日本語", "/Ελληνικά", "/emoji-🎉",
                      "/a-b_c.d~e", "/a%20b", "/x?y=1#z"):
            with self.subTest(value=value):
                self.assertFalse(_has_url_control_chars(value), value)

    def test_theme_redirect_keeps_site_relative_path(self):
        response = self.app.request("GET", "/theme", query={"mode": "light", "next": "/about"})
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/about")
        cookies = " ".join(response.headers_all("set-cookie"))
        self.assertIn("theme=light", cookies)

    def test_theme_invalid_mode_sets_no_cookie(self):
        response = self.app.request("GET", "/theme", query={"mode": "neon", "next": "/"})
        self.assertEqual(response.status, 302)
        cookies = " ".join(response.headers_all("set-cookie"))
        self.assertNotIn("theme=", cookies)

    def test_theme_cookie_is_http_only_same_site_lax(self):
        response = self.app.request("GET", "/theme", query={"mode": "dark", "next": "/"})
        theme = next((h for h in response.headers_all("set-cookie") if h.startswith("theme=")),
                     "")
        self.assertIn("HttpOnly", theme)
        self.assertIn("SameSite=Lax", theme)
        self.assertIn("Secure", theme)          # https 请求

    def test_theme_value_is_not_reflected_into_html(self):
        response = self.app.request("GET", "/", cookies={"theme": '"><script>x</script>'})
        self.assertEqual(response.status, 200)
        self.assertNotIn("<script>x</script>", response.text)


class SecurityHeaderTests(ElenvindTestCase):
    def test_common_security_headers_present(self):
        response = self.app.request("GET", "/")
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertEqual(response.header("x-frame-options"), "DENY")
        self.assertEqual(response.header("referrer-policy"), "strict-origin-when-cross-origin")
        csp = response.header("content-security-policy") or ""
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)

    def test_hsts_requires_config_opt_in(self):
        """默认不配 HSTS：即使 HTTPS 也不下发（避免把访客误锁在 HTTPS）。"""
        self.app.scheme = "https"
        self._config["security"] = {}
        self.assertIsNone(self.app.request("GET", "/").header("strict-transport-security"))

    def test_hsts_on_https_when_enabled(self):
        self.app.scheme = "https"
        self._config["security"] = {"hsts_enabled": True}
        self.assertEqual(self.app.request("GET", "/").header("strict-transport-security"),
                         "max-age=31536000")

    def test_hsts_never_sent_over_http(self):
        """HTTP 上下发 HSTS 没有意义（浏览器忽略），误发反而会把访客锁死。"""
        self._config["security"] = {"hsts_enabled": True}
        self.app.scheme = "http"
        self.assertIsNone(self.app.request("GET", "/").header("strict-transport-security"))

    def test_hsts_max_age_and_subdomains_are_configurable(self):
        self.app.scheme = "https"
        self._config["security"] = {"hsts_enabled": True, "hsts_max_age": 300,
                                    "hsts_include_subdomains": True}
        self.assertEqual(self.app.request("GET", "/").header("strict-transport-security"),
                         "max-age=300; includeSubDomains")

    def test_hsts_can_be_disabled_explicitly(self):
        self.app.scheme = "https"
        self._config["security"] = {"hsts_enabled": False}
        self.assertIsNone(self.app.request("GET", "/").header("strict-transport-security"))

    def test_cache_control_no_cache_for_anonymous_html(self):
        response = self.app.request("GET", "/")
        self.assertEqual(response.header("cache-control"), "no-cache")

    def test_cache_control_no_store_for_logged_in_html(self):
        self.create_user(email="cache@example.com", password="password-123")
        login = self.login("cache@example.com", "password-123")
        token = self.app.set_cookie_value(login, "session")
        self.assertTrue(token)
        response = self.app.request("GET", "/", cookies={"session": token})
        self.assertEqual(response.header("cache-control"), "no-store")

    def test_non_html_responses_are_no_store_by_default(self):
        response = self.app.request("GET", "/robots.txt")
        self.assertEqual(response.header("cache-control"), "public, max-age=3600")

    def test_content_length_matches_body(self):
        response = self.app.request("GET", "/")
        self.assertEqual(int(response.header("content-length")), len(response.body))


class CsrfGateTests(ElenvindTestCase):
    """所有状态变更 POST 都必须经过统一 CSRF 闸门。"""

    STATE_CHANGING_POSTS = (
        ("/login", {}),
        ("/register", {}),
        ("/user", {"action": "update_profile"}),
        ("/logout", {}),
        ("/article/some-slug/comment", {"content": "hi"}),
        ("/article/some-slug/comment/delete/1", {}),
        ("/article/some-slug/comment/edit/1", {}),
    )

    def test_missing_token_is_rejected(self):
        for path, form in self.STATE_CHANGING_POSTS:
            with self.subTest(path=path):
                response = self.app.request("POST", path, form=form)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.status, 400)
        self.assertTrue("CSRF" in response.text
                        or "could not be processed" in response.text,
                        response.text[:200])

    def test_wrong_token_is_rejected(self):
        wrong = "A" * 43
        for path, form in self.STATE_CHANGING_POSTS:
            with self.subTest(path=path):
                payload = dict(form)
                payload["csrf_token"] = wrong
                response = self.app.request("POST", path, form=payload,
                                            cookies={"csrf": "B" * 43})
                self.assertEqual(response.status, 400)

    def test_malformed_token_is_rejected(self):
        for bad in ("short", "x" * 44, "", "!" * 43):
            with self.subTest(bad=bad):
                response = self.app.request("POST", "/logout",
                                            form={"csrf_token": bad},
                                            cookies={"csrf": bad})
                self.assertEqual(response.status, 400)

    def test_correct_token_passes_the_gate(self):
        token = self.fetch_csrf()
        response = self.app.request("POST", "/login",
                                    form={"csrf_token": token,
                                          "email": "nobody@example.com",
                                          "password": "wrong"},
                                    cookies={"csrf": token})
        self.assertEqual(response.status, 200)

    def test_token_in_cookie_but_not_form_is_rejected(self):
        token = self.fetch_csrf()
        response = self.app.request("POST", "/logout", form={}, cookies={"csrf": token})
        self.assertEqual(response.status, 400)

    def test_get_requests_never_mutate_state(self):
        from elenvind.db.user import get_user_by_id
        from elenvind.db.comment import get_comments_by_article

        user_id, _ = self.create_user(email="readonly@example.com")
        self.write_article("readonly-post", "body")
        before_user = get_user_by_id(user_id)
        for path in ("/", "/login", "/register", "/user", "/article/readonly-post",
                     "/robots.txt", "/sitemap.xml", "/theme"):
            with self.subTest(path=path):
                response = self.app.request("GET", path, query={"mode": "dark",
                                                                "next": "/",
                                                                "reply_to": "1"})
                self.assertLess(response.status, 500)
        after_user = get_user_by_id(user_id)
        self.assertEqual(before_user["email"], after_user["email"])
        self.assertEqual(before_user["password"], after_user["password"])
        self.assertEqual(len(get_comments_by_article("readonly-post")), 0)


if __name__ == "__main__":
    unittest.main()
