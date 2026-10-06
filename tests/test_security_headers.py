"""统一安全响应头（Core 注入）的契约测试。

验收要点：
1. 所有响应 —— 含 404 / 500 错误页 —— 都带 nosniff / referrer-policy；
2. HTTPS + 配置启用才下发 HSTS；
3. CSP 可通过配置关闭或修改；
4. 默认 CSP（default-src 'self'）下页面正常渲染，且没有任何 inline script。
"""
import re
import unittest

from tests.support import PROJECT_ROOT, ElenvindTestCase

from elenvind.core import security
from elenvind.core.config import ConfigError, validate_config

#: 每个响应都必须带的头（顺序无关）
ALWAYS_ON = (
    "x-content-type-options",
    "referrer-policy",
    "content-security-policy",
    "permissions-policy",
)


class SecurityHeaderPresenceTests(ElenvindTestCase):
    """验收 1：所有响应类型都带头，包括错误页。"""

    PATHS = ("/", "/login", "/register", "/about", "/robots.txt", "/sitemap.xml",
             "/user",
             # 404
             "/definitely-not-here",
             "/article/no-such-article",
             # 静态资源
             "/css/style.css",
             "/imgs/favicon.ico")

    def test_headers_present_on_every_response(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                for header in ALWAYS_ON:
                    self.assertIsNotNone(response.header(header),
                                         f"{header} missing on {path} "
                                         f"({response.status})")

    def test_headers_present_on_query_route(self):
        response = self.app.request("GET", "/theme",
                                    query={"mode": "dark", "next": "/"})
        self.assertEqual(response.status, 302)
        for header in ALWAYS_ON:
            self.assertIsNotNone(response.header(header),
                                 f"{header} missing on /theme ({response.status})")

    def test_404_page_carries_headers(self):
        response = self.app.request("GET", "/definitely-not-here")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertEqual(response.header("referrer-policy"),
                         "strict-origin-when-cross-origin")
        self.assertIn("default-src 'self'", response.header("content-security-policy"))

    def test_405_response_carries_headers(self):
        response = self.app.request("PUT", "/")
        self.assertIn(response.status, (400, 405))
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertIsNotNone(response.header("content-security-policy"))

    def test_500_error_page_carries_headers(self):
        """500 走 App 的异常处理路径，也必须带全套头。

        用独立的 App 实例走完整 WSGI 路径（与 test_core_contract 里
        "handler 忘记 return" 的写法一致），避免污染共享路由表。
        """
        from elenvind.core.app import App
        from tests.support import build_environ, call_wsgi

        mini = App()

        def boom(request):
            raise RuntimeError("intentional failure for header test")

        mini.router.route("/boom", methods=["GET"])(boom)

        with self.assertLogs("elenvind.core.app", level="ERROR"):
            response = call_wsgi(mini, build_environ("GET", "/boom"))

        self.assertEqual(response.status, 500)
        headers = {key.decode().lower(): value.decode()
                   for key, value in response.headers}
        for header in ALWAYS_ON:
            self.assertIn(header, headers, f"{header} missing on 500 response")
        self.assertEqual(headers["referrer-policy"], "strict-origin-when-cross-origin")

    def test_early_error_carries_headers(self):
        """请求对象还不可用时的早期错误也要带头（畸形 Content-Length）。"""
        response = self.app.raw_request(
            "POST", "/login", b"",
            [("host", "example.com"), ("content-length", "not-a-number")],
            body=b"")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertEqual(response.header("referrer-policy"),
                         "strict-origin-when-cross-origin")
        self.assertIn("default-src 'self'",
                      response.header("content-security-policy") or "")
        self.assertIn("camera=()", response.header("permissions-policy") or "")

    def test_early_error_omits_hsts_by_default(self):
        """早期错误路径同样遵守"HSTS 需显式启用"。"""
        response = self.app.raw_request(
            "POST", "/login", b"",
            [("host", "example.com"), ("content-length", "not-a-number")],
            body=b"")
        self.assertIsNone(response.header("strict-transport-security"))

    def test_redirect_carries_headers(self):
        response = self.app.request("GET", "/theme",
                                    query={"mode": "dark", "next": "/login"})
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertIsNotNone(response.header("content-security-policy"))


class CspDefaultsTests(ElenvindTestCase):
    """验收 4：默认 CSP 内容 + 页面无 inline script。"""

    def test_default_csp_shape(self):
        csp = self.app.request("GET", "/").header("content-security-policy")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("form-action 'self'", csp)
    def test_style_src_allows_the_inline_hero_style(self):
        """`style-src` 必须含 'unsafe-inline'。

        回归：曾经把默认值收紧成 `style-src 'self'`。CSP 的 `style-src`
        同时管 `<style>` 元素与 `style=` **属性**，而首页 hero 的后台图
        正是靠 `style="background-image:url(…)"` 设置的，于是 hero 图
        静默失效并在控制台刷 style-src-elem 报错。

        这里同时断言"没有 `<style>` 元素"——说明放开 'unsafe-inline' 只服务于
        那一个 style 属性，不是因为模板里混进了样式块。
        """
        csp = self.app.request("GET", "/").header("content-security-policy")
        style_src = [part for part in csp.split("; ") if part.startswith("style-src")]
        self.assertEqual(len(style_src), 1)
        self.assertEqual(style_src[0], "style-src 'self' 'unsafe-inline'")
        # 'unsafe-eval' 与脚本相关的放宽仍然必须没有
        self.assertNotIn("unsafe-eval", csp)
        self.assertIn("script-src 'none'", csp)

    def test_hero_style_attribute_actually_renders(self):
        """hero 配了就该渲染出那个 style 属性（CSP 已放行它）。"""
        self._config.setdefault("static", {})["hero"] = "https://e.com/hero.jpg"
        html = self.app.request("GET", "/").text
        self.assertIn("class=\"hero\"", html)
        self.assertIn("style=\"background-image:url('https://e.com/hero.jpg')\"",
                      html)

    def test_style_src_covers_the_site_stylesheet(self):
        """样式表必须同源，否则 style-src 'self' 会把它拦掉。"""
        csp = self.app.request("GET", "/").header("content-security-policy")
        style_src = [part for part in csp.split("; ") if part.startswith("style-src")]
        self.assertEqual(len(style_src), 1)
        self.assertIn("'self'", style_src[0])
        self.assertTrue(security.stylesheet_is_same_origin())

    def test_rendered_pages_contain_no_inline_script(self):
        """验收 4 的核心：默认 CSP 下页面不会被自己的内联脚本挡住。"""
        for path in ("/", "/login", "/register", "/about", "/user",
                     "/article/no-such-article"):
            with self.subTest(path=path):
                html = self.app.request("GET", path).text
                self.assertNotIn("<script", html.lower(),
                                 f"{path} rendered an inline <script>")
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", html),
                                  f"{path} rendered an inline event handler")
                self.assertNotIn("<style", html.lower(),
                                 f"{path} rendered an inline <style> block")
    def test_only_inline_style_is_the_config_hero(self):
        """内联 style 属性只允许出现在 hero（值来自 config 且已校验）。"""
        html = self.app.request("GET", "/").text
        for match in re.finditer(r'style="([^"]*)"', html):
            with self.subTest(value=match.group(1)):
                self.assertTrue(match.group(1).startswith("background-image:url("),
                                f"unexpected inline style: {match.group(1)}")


class CspConfigTests(ElenvindTestCase):
    """验收 3：CSP 可关闭 / 可修改。"""

    def test_csp_can_be_disabled(self):
        self._config["security"] = {"csp_enabled": False}
        response = self.app.request("GET", "/")
        self.assertIsNone(response.header("content-security-policy"))
        # 关掉 CSP 不影响其它安全头
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertEqual(response.header("referrer-policy"),
                         "strict-origin-when-cross-origin")

    def test_csp_directive_can_be_tightened(self):
        self._config["security"] = {"csp": {"img-src": ["'self'", "data:"]}}
        csp = self.app.request("GET", "/").header("content-security-policy")
        img_src = [part for part in csp.split("; ") if part.startswith("img-src")]
        self.assertEqual(img_src, ["img-src 'self' data:"])
        # 收紧了 img-src，就不该再放行任意外部图片
        self.assertNotIn("http:", img_src[0])

    def test_csp_directive_can_be_removed(self):
        self._config["security"] = {"csp": {"object-src": False}}
        csp = self.app.request("GET", "/").header("content-security-policy")
        self.assertNotIn("object-src", csp)

    def test_csp_directive_accepts_string_form(self):
        self._config["security"] = {"csp": {"style-src": "'self' 'unsafe-inline'"}}
        csp = self.app.request("GET", "/").header("content-security-policy")
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)

    def test_permissions_policy_can_be_disabled_and_replaced(self):
        self._config["security"] = {"permissions_policy": ""}
        self.assertIsNone(self.app.request("GET", "/").header("permissions-policy"))
        self._config["security"] = {"permissions_policy": "camera=(), microphone=()"}
        self.assertEqual(self.app.request("GET", "/").header("permissions-policy"),
                         "camera=(), microphone=()")

    def test_default_permissions_policy_closes_sensitive_features(self):
        policy = self.app.request("GET", "/").header("permissions-policy")
        for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
            with self.subTest(feature=feature):
                self.assertIn(f"{feature}=()", policy)


class SecurityHeaderConfigValidationTests(unittest.TestCase):
    """配置校验：坏值必须在启动时就被拒绝（而不是拼出畸形响应头）。"""

    def setUp(self):
        from elenvind.core import config as config_module
        from elenvind.core.templating import DEFAULT_TEMPLATES_DIR
        self.config_module = config_module
        self.base = {
            "title": "T",
            "site_url": "https://example.com",
            "articles_dir": str(DEFAULT_TEMPLATES_DIR.parent / "templates"),
        }

    def _validate(self, security_cfg):
        cfg = dict(self.base)
        cfg["security"] = security_cfg
        original = dict(self.config_module.config)
        self.config_module.config.clear()
        self.config_module.config.update(cfg)
        try:
            validate_config()
        finally:
            self.config_module.config.clear()
            self.config_module.config.update(original)

    def test_bad_boolean_is_rejected(self):
        for key in ("csp_enabled", "hsts_enabled", "hsts_include_subdomains"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    self._validate({key: "yes"})

    def test_bad_hsts_max_age_is_rejected(self):
        for bad in ("soon", -1, 999999999, True):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    self._validate({"hsts_max_age": bad})

    def test_control_characters_in_csp_are_rejected(self):
        """否则可以借配置值伪造额外的响应头。"""
        with self.assertRaises(ConfigError):
            self._validate({"csp": {"img-src": ["'self'\r\nX-Evil: 1"]}})
        with self.assertRaises(ConfigError):
            self._validate({"permissions_policy": "camera=()\r\nX-Evil: 1"})

    def test_invalid_directive_name_is_rejected(self):
        for bad in ("img src", "img_src", "IMG-SRC", "", "img:src"):
            with self.subTest(name=bad):
                with self.assertRaises(ConfigError):
                    self._validate({"csp": {bad: ["'self'"]}})

    def test_bad_directive_value_type_is_rejected(self):
        with self.assertRaises(ConfigError):
            self._validate({"csp": {"img-src": [1, 2]}})
        with self.assertRaises(ConfigError):
            self._validate({"csp": {"img-src": {"a": 1}}})

    def test_reasonable_values_are_accepted(self):
        self._validate({"csp_enabled": False})
        self._validate({"hsts_enabled": True, "hsts_max_age": 0,
                        "hsts_include_subdomains": True})
        self._validate({"csp": {"img-src": ["'self'", "data:"],
                                "object-src": False,
                                "style-src": "'self'"}})


class ShippedConfigStructureTests(unittest.TestCase):
    """打包的两份 config 必须结构正确。

    回归：`[security]` / `[security.csp]` 曾被插在顶层键**之前**，
    于是 TOML 把它们后面的 `max_body_size` / `database` / 各目录键
    全都吞进了 `[security]` 表——配置看起来"消失了"，而且不报错。
    这类错误必须在提交前被抓住。
    """

    #: 必须是顶层标量的键（代表性的几个）
    TOP_LEVEL = ("max_body_size", "database", "articles_dir", "custom_pages_dir",
                 "templates_dir", "use_builtin_css", "title", "site_url",
                 "admin_user_id", "registration_enabled")

    #: [security] 里允许出现的键
    SECURITY_KEYS = frozenset({"csp_enabled", "permissions_policy", "hsts_enabled",
                               "hsts_max_age", "hsts_include_subdomains", "csp"})

    def _load(self, name):
        import tomllib
        with open(PROJECT_ROOT / name, "rb") as handle:
            return tomllib.load(handle)

    def test_expected_keys_are_top_level(self):
        for name in ("config.toml", "config.example.toml"):
            data = self._load(name)
            for key in self.TOP_LEVEL:
                with self.subTest(config=name, key=key):
                    self.assertIn(key, data,
                                  f"{name} 的 {key} 不在顶层（被某张表吃掉了？）")

    def test_no_top_level_key_leaked_into_security_table(self):
        for name in ("config.toml", "config.example.toml"):
            data = self._load(name)
            security = data.get("security") or {}
            leaked = sorted(set(self.TOP_LEVEL) & set(security))
            self.assertEqual(leaked, [],
                             f"{name} 的 [security] 吞掉了顶层键: {leaked}")

    def test_security_table_contains_only_security_keys(self):
        for name in ("config.toml", "config.example.toml"):
            data = self._load(name)
            security = data.get("security")
            if security is None:
                continue
            with self.subTest(config=name):
                self.assertEqual(sorted(set(security) - self.SECURITY_KEYS), [])

    def test_security_table_is_not_buried_in_another_table(self):
        """[security] 必须是顶层表，不能变成 [something.security]。"""
        for name in ("config.toml", "config.example.toml"):
            data = self._load(name)
            with self.subTest(config=name):
                self.assertIsInstance(data.get("security"), dict)
                for table_name, table in data.items():
                    if not isinstance(table, dict) or table_name == "security":
                        continue
                    self.assertNotIn("security", table,
                                     f"{name}: [security] 被埋进了 [{table_name}]")

    def test_shipped_configs_pass_validation(self):
        """两份配置都要能通过校验（含 [security] 的类型/取值检查）。"""
        import tomllib
        from elenvind.core import config as config_module
        original = dict(config_module.config)
        try:
            for name in ("config.toml", "config.example.toml"):
                with open(PROJECT_ROOT / name, "rb") as handle:
                    data = tomllib.load(handle)
                config_module.config.clear()
                config_module.config.update(data)
                with self.subTest(config=name):
                    validate_config()
        finally:
            config_module.config.clear()
            config_module.config.update(original)


if __name__ == "__main__":
    unittest.main()
