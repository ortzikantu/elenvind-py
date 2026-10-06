"""Cookie 下发策略。

覆盖 P1-4（Cookie 职责在 Core）与 P1-5（会话 Cookie 不上 `Cache-Control: public`
响应）两处修复 —— 这两项在修复时只有一次性验证脚本，**没有任何常驻测试**，
所以随时可能被改回去而没人发现。

背景（P1-5）：静态资源与 robots/sitemap 是 `Cache-Control: public`，
若在这些响应里回带 `Set-Cookie: session=…`，任何"会缓存 Set-Cookie"的
中间层（CDN 缓存一切、nginx `proxy_ignore_headers Set-Cookie` 之类）
都可能把某人的会话 token 回放给其他访客。
"""
from tests.support import ElenvindTestCase

#: 会带 `Cache-Control: public` 的路径（可被中间层缓存）
PUBLIC_PATHS = ("/css/style.css", "/imgs/favicon.ico", "/imgs/logo.png",
                "/robots.txt", "/sitemap.xml")


def session_headers(response):
    return [h for h in response.headers_all("set-cookie")
            if h.startswith("session=")]


def theme_headers(response):
    return [h for h in response.headers_all("set-cookie") if h.startswith("theme=")]


class PublicResponseCookieTests(ElenvindTestCase):
    """P1-5：可被公开缓存的响应不得回带会话 Cookie。"""

    def _logged_in_jar(self):
        self.create_user(nickname="Ann", email="ann@example.com")
        session, csrf = self.login_ok("ann@example.com", "correct horse battery")
        return self.app_cookies(session=session, csrf=csrf)

    def test_public_paths_are_actually_public(self):
        """前置条件：这些路径确实声明了 public 缓存（否则本测试没有意义）。"""
        for path in PUBLIC_PATHS:
            with self.subTest(path=path):
                cache = self.app.request("GET", path).header("cache-control") or ""
                self.assertTrue(cache.startswith("public"),
                                f"{path} 不再是 public 缓存：{cache!r}")

    def test_no_session_cookie_on_public_responses(self):
        jar = self._logged_in_jar()
        for path in PUBLIC_PATHS:
            with self.subTest(path=path):
                response = self.app.request("GET", path, cookies=jar)
                self.assertEqual(
                    session_headers(response), [],
                    f"{path} 回带了会话 Cookie（可被中间层缓存并回放给他人）")

    def test_no_session_cookie_even_for_anonymous_requests(self):
        """匿名请求同样不该出现会话 Cookie（没有会话就不该有 Cookie）。"""
        for path in PUBLIC_PATHS:
            with self.subTest(path=path):
                self.assertEqual(session_headers(self.app.request("GET", path)), [])

    def test_login_still_issues_the_session_cookie(self):
        """反向确认：会话**变化**时依然要下发（不能修成"永不发 Cookie"）。"""
        self.create_user(nickname="Ann", email="ann@example.com")
        page = self.app.request("GET", "/login")
        csrf = self.app.set_cookie_value(page, self.csrf_cookie_name())
        response = self.app.request("POST", "/login",
                                    form={"csrf_token": csrf,
                                          "email": "ann@example.com",
                                          "password": "correct horse battery"},
                                    cookies={self.csrf_cookie_name(): csrf})
        self.assertEqual(response.status, 302)
        headers = session_headers(response)
        self.assertTrue(headers, "登录没有下发会话 Cookie")
        self.assertNotIn("Max-Age=0", headers[0], "登录下发的 Cookie 不该立即过期")

    def test_session_cookie_is_not_reissued_on_every_page_view(self):
        """用已有会话访问页面不该重复下发（会话没变化就不发）。"""
        jar = self._logged_in_jar()
        for path in ("/", "/about"):
            with self.subTest(path=path):
                response = self.app.request("GET", path, cookies=jar)
                self.assertEqual(
                    session_headers(response), [],
                    f"{path} 在没有会话变化时重复下发了会话 Cookie")

    def test_logout_clears_the_session_cookie(self):
        """登出必须下发**清除**指令（而不是留着失效 token 或重新写回）。"""
        self.create_user(nickname="Ann", email="ann@example.com")
        session, csrf = self.login_ok("ann@example.com", "correct horse battery")
        response = self.app.request(
            "POST", "/logout", form={"csrf_token": csrf},
            cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 302)
        headers = session_headers(response)
        self.assertTrue(headers, "登出没有清除会话 Cookie")
        for header in headers:
            self.assertIn("Max-Age=0", header, header)

    def test_cookie_attributes_are_hardened(self):
        """所有下发的 Cookie 都必须带 HttpOnly / SameSite=Lax / Path=/。"""
        self.create_user(nickname="Ann", email="ann@example.com")
        session, csrf = self.login_ok("ann@example.com", "correct horse battery")
        response = self.app.request("GET", "/theme",
                                    query={"mode": "dark", "next": "/"},
                                    cookies=self.app_cookies(session=session,
                                                             csrf=csrf))
        headers = response.headers_all("set-cookie")
        self.assertTrue(headers)
        for header in headers:
            with self.subTest(header=header):
                self.assertIn("HttpOnly", header)
                self.assertIn("SameSite=Lax", header)
                self.assertIn("Path=/", header)


class ThemePreferenceCookieTests(ElenvindTestCase):
    """P1-4：偏好 Cookie 由 Core 按白名单下发（模块只说"设成 dark"）。"""

    def test_valid_modes_are_written(self):
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                response = self.app.request("GET", "/theme",
                                            query={"mode": mode, "next": "/about"})
                headers = theme_headers(response)
                self.assertTrue(headers, f"mode={mode} 没有写入偏好 Cookie")
                self.assertTrue(any(h.startswith(f"theme={mode}") for h in headers))

    def test_invalid_modes_are_not_written(self):
        for mode in ("neon", "", "'; DROP TABLE user;--", "DARK", "dark "):
            with self.subTest(mode=mode):
                response = self.app.request("GET", "/theme",
                                            query={"mode": mode, "next": "/"})
                self.assertEqual(theme_headers(response), [],
                                 f"非法 mode={mode!r} 被写进了 Cookie")

    def test_forged_cookie_value_is_not_echoed_into_markup(self):
        """读取侧：伪造的 theme Cookie 不得变成真实标签注入页面。"""
        from html.parser import HTMLParser

        class Collector(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.tags = []

            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)

        def tags_of(html):
            collector = Collector()
            collector.feed(html)
            return collector.tags

        baseline = tags_of(self.app.request("GET", "/").text)
        for forged in ('"><script>alert(1)</script>', "'><img src=x onerror=alert(1)>",
                       "<svg onload=alert(1)>"):
            with self.subTest(forged=forged):
                text = self.app.request("GET", "/", cookies={"theme": forged}).text
                tags = tags_of(text)
                for tag in ("script", "img", "svg"):
                    self.assertLessEqual(
                        tags.count(tag), baseline.count(tag),
                        f"{tag} 标签数量增加 —— 伪造的 theme Cookie 注入成功")
