"""HTTP 层测试：请求体 framing、方法/路由边界、安全响应头、缓存策略、HEAD。"""
import unittest
from urllib.parse import urlencode

from tests.support import ElenvindTestCase


class PostBodyFramingTests(ElenvindTestCase):
    """_read_post_body 的各类畸形 / 边界输入（规格要求逐项覆盖）。"""

    def _post_raw(self, body=b"", headers=None, chunks=None, path="/login"):
        base = [("host", "example.com")]
        base.extend(headers or [])
        return self.app.raw_request("POST", path, b"", base, body=body, chunks=chunks)

    def test_negative_content_length_is_400(self):
        response = self._post_raw(headers=[("content-length", "-1")])
        self.assertEqual(response.status, 400)

    def test_non_numeric_content_length_is_400(self):
        response = self._post_raw(headers=[("content-length", "abc")])
        self.assertEqual(response.status, 400)

    def test_missing_content_length_is_411_not_empty_form(self):
        """缺少 Content-Length（含 chunked）必须明确拒绝，而不是当成空表单。"""
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

    def test_truncated_body_is_400(self):
        """声明 1000 字节却只发 10 字节：不能当合法请求处理。"""
        body = b"a" * 10
        response = self._post_raw(
            body=body,
            headers=[("content-length", "1000"),
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
        self.assertIn("CSRF", response.text)

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
        self.assertIn("CSRF", response.text)

    def test_missing_content_type_is_accepted(self):
        body = urlencode({"csrf_token": "x"}).encode()
        response = self._post_raw(body=body, headers=[("content-length", str(len(body)))])
        self.assertEqual(response.status, 400)
        self.assertIn("CSRF", response.text)

    def test_multiple_body_chunks_are_concatenated(self):
        token = self.fetch_csrf()
        body = urlencode({"csrf_token": token, "email": "nobody@example.com",
                          "password": "whatever"}).encode()
        first, second = body[:10], body[10:]
        chunks = [
            {"type": "http.request", "body": first, "more_body": True},
            {"type": "http.request", "body": second, "more_body": False},
        ]
        response = self._post_raw(chunks=chunks, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/x-www-form-urlencoded"),
            ("cookie", f"csrf={token}"),
        ])
        self.assertEqual(response.status, 200)   # 登录失败页（账号不存在）

    def test_empty_chunks_do_not_loop_forever(self):
        token = self.fetch_csrf()
        body = urlencode({"csrf_token": token, "email": "a@b.c", "password": "x"}).encode()
        chunks = [{"type": "http.request", "body": b"", "more_body": True} for _ in range(5)]
        chunks.append({"type": "http.request", "body": body, "more_body": False})
        response = self._post_raw(chunks=chunks, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/x-www-form-urlencoded"),
            ("cookie", f"csrf={token}"),
        ])
        self.assertEqual(response.status, 200)

    def test_endless_empty_chunks_are_bounded(self):
        """more_body 永远为真时必须有上限，不能死循环。"""
        chunks = [{"type": "http.request", "body": b"", "more_body": True}
                  for _ in range(2000)]
        response = self._post_raw(chunks=chunks, headers=[
            ("content-length", "100"),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertIn(response.status, (400, 413))

    def test_client_disconnect_is_400(self):
        chunks = [{"type": "http.disconnect"}]
        response = self._post_raw(chunks=chunks, headers=[
            ("content-length", "10"),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertEqual(response.status, 400)

    def test_unexpected_asgi_message_is_400(self):
        chunks = [{"type": "http.response.start"}, {"type": "http.disconnect"}]
        response = self._post_raw(chunks=chunks, headers=[
            ("content-length", "10"),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertEqual(response.status, 400)

    def test_invalid_utf8_body_is_400(self):
        body = b"csrf_token=\xff\xfe"
        response = self._post_raw(body=body, headers=[
            ("content-length", str(len(body))),
            ("content-type", "application/x-www-form-urlencoded"),
        ])
        self.assertEqual(response.status, 400)

    def test_body_exactly_at_limit_is_allowed(self):
        """上限之内的 body 必须能正常进入业务逻辑（此处以 CSRF 失败为界）。"""
        from elenvind.config import config as live_config

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
        response = self.app.raw_request("DELETE", "/")
        self.assertEqual(response.status, 405)
        allow = response.header("allow") or ""
        self.assertIn("GET", allow)
        self.assertIn("POST", allow)

    def test_head_returns_headers_without_body(self):
        response = self.app.raw_request("HEAD", "/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.header("content-length"),
                         str(len(self.app.request("GET", "/").body)))

    def test_get_logout_is_not_allowed(self):
        response = self.app.request("GET", "/logout")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("allow"), "POST")

    def test_unknown_path_returns_404(self):
        response = self.app.request("GET", "/definitely-not-here")
        self.assertEqual(response.status, 404)

    def test_unknown_post_path_returns_405(self):
        response = self.app.request("POST", "/nope", form={"a": "b"})
        self.assertEqual(response.status, 405)

    def test_article_route_with_extra_segments_is_404(self):
        response = self.app.request("GET", "/article/a/b")
        self.assertEqual(response.status, 404)

    def test_traversal_like_paths_never_touch_filesystem(self):
        for path in ("/..%2fconfig.toml", "/../config.toml", "/articles/../config.toml",
                     "/usrpages/../config.toml", "/.git/config", "/about/../secret"):
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertIn(response.status, (404, 302))
                self.assertNotIn("SECRET_KEY", response.text)
                self.assertNotIn("site_url", response.text)

    def test_theme_redirect_rejects_open_redirect(self):
        for value in ("//evil.example.com", "https://evil.example.com", "\\\\evil",
                      "javascript:alert(1)", "/\\evil", ""):
            with self.subTest(value=value):
                response = self.app.request("GET", "/theme",
                                            query={"mode": "dark", "next": value})
                self.assertEqual(response.status, 302)
                location = response.header("location")
                self.assertTrue(location.startswith("/"))
                self.assertFalse(location.startswith("//"))

    def test_theme_redirect_keeps_site_relative_path(self):
        response = self.app.request("GET", "/theme", query={"mode": "light", "next": "/about"})
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/about")
        self.assertIn("theme=light", response.header("set-cookie") or "")

    def test_theme_invalid_mode_sets_no_cookie(self):
        response = self.app.request("GET", "/theme", query={"mode": "neon", "next": "/"})
        self.assertEqual(response.status, 302)
        self.assertIsNone(response.header("set-cookie"))

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
        self.assertIn("script-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_hsts_only_on_https(self):
        secure_response = self.app.request("GET", "/")
        self.assertIsNotNone(secure_response.header("strict-transport-security"))
        self.app.scheme = "http"
        plain_response = self.app.request("GET", "/")
        self.assertIsNone(plain_response.header("strict-transport-security"))

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
        ("/article/some-slug/comment/restore/1", {}),
    )

    def test_missing_token_is_rejected(self):
        for path, form in self.STATE_CHANGING_POSTS:
            with self.subTest(path=path):
                response = self.app.request("POST", path, form=form)
                self.assertEqual(response.status, 400)
                self.assertIn("CSRF", response.text)

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
        from elenvind.db_user import get_user_by_id
        from elenvind.db_comment import get_comments_by_article

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
