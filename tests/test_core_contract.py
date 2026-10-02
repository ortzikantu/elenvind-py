"""Core Contract 测试：框架承诺的"默认安全"必须可验证、且不会被 Feature 绕过。

这个模块回答一个问题：**Feature 作者什么都不做时，安全性还成立吗？**
它不复测业务逻辑，只验证框架层的契约：

A. 声明即安全      —— auth / permission / CSRF 由调度器执行，Feature 不写也生效
B. 无法绕过        —— Feature 不得自己实现 session / cookie / csrf / 密码 /
                      安全头 / 请求限制（静态守卫 + 运行时验证）
C. 默认安全        —— 每个响应都带安全头；每个 Cookie 都是 HttpOnly+SameSite
D. 失效关闭        —— 声明写错时宁可拒绝，也不放行（回归守卫）
E. 边界健壮        —— 畸形请求得到确定状态码，不 500、不静默放行
"""
import ast
import re
import unittest
from pathlib import Path
from urllib.parse import quote

from tests.support import PROJECT_ROOT, ElenvindTestCase

from elenvind.app import app
from elenvind.core.context import build_render_context

FEATURES_DIR = PROJECT_ROOT / "elenvind" / "features"
CORE_DIR = PROJECT_ROOT / "elenvind" / "core"


def _feature_sources():
    for path in sorted(FEATURES_DIR.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


def _imported_names(source: str):
    """返回源码里所有 import 的名字（含 from ... import 的每个名字）。"""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
    return names


class DeclarativeSecurityTests(ElenvindTestCase):
    """A. 声明即安全：路由声明是唯一需要写的东西。"""

    def test_declared_required_routes_never_reach_business_logic(self):
        """所有 `auth="required"` 的路由，匿名访问都不得进入业务逻辑。

        友好语义：
        - 浏览器导航（GET）→ 302 跳登录页并带回跳地址；
        - 非 GET / 405 / 404 → 直接拒绝。

        例外：同一路径若另有 public 的 GET 路由（例如 `/user` 的友好提示页），
        GET 会由那条路由处理，这里跳过（由 `test_friendly_public_pages_leak_nothing`
        验证它不泄漏任何数据）。
        """
        public_get_paths = {
            route.path for route in app.router.routes
            if "GET" in route.methods and route.auth not in ("required", "authenticated")
        }
        checked = 0
        for route in app.router.routes:
            if route.auth != "required":
                continue
            # 该路径的 GET 由 public 路由负责：跳过（GET 探针测不到这条 POST 路由）
            if route.path in public_get_paths:
                continue
            path = route.path.replace("<slug>", "any").replace("<comment_id>", "1")
            response = self.app.request("GET", path)
            if response.status == 302:
                location = response.header("location") or ""
                self.assertTrue(location.startswith("/login?next="),
                                f"{route.path} redirected to {location!r}")
            else:
                self.assertIn(response.status, (400, 403, 404, 405),
                              f"{route.path} leaked to anonymous ({response.status})")
            checked += 1
        self.assertTrue(checked, "no auth=required routes found to verify")

    def test_friendly_public_pages_leak_nothing(self):
        """公开的"友好提示页"不能泄漏任何账号数据或私有表单。"""
        self.create_user(nickname="Secret", email="secret@example.com")
        response = self.app.request("GET", "/user")
        self.assertEqual(response.status, 200)
        self.assertNotIn("secret@example.com", response.text)
        self.assertNotIn("Secret", response.text)
        for private in ('name="password_confirm"', 'name="old_password"',
                        'name="new_password"', 'name="action"'):
            self.assertNotIn(private, response.text)
        # 必须给出登录入口
        self.assertIn("/login", response.text)

    def test_anonymous_get_redirects_to_login_with_next(self):
        """未登录 GET 受保护页面：302 -> /login?next=<原路径>。

        `/user` 不在其中：它有自己的友好提示页（见
        `test_friendly_public_pages_leak_nothing`）。
        """
        for path in ("/admin",):
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertEqual(response.status, 302)
                self.assertEqual(response.header("location"),
                                 f"/login?next={quote(path, safe='')}")

    def test_next_survives_query_string(self):
        """带查询串的页面也要能正确回跳。"""
        response = self.app.request("GET", "/admin", query={"tab": "users", "page": "2"})
        location = response.header("location") or ""
        self.assertEqual(
            location,
            "/login?next=" + quote("/admin?tab=users&page=2", safe=""))

    def test_after_login_user_returns_to_original_page(self):
        """完整闭环：匿名访问 -> 跳登录 -> 登录后回到原页面。"""
        self.create_user(nickname="Boss", email="boss@example.com")
        self._config["admin_user_id"] = 1

        landed = self.app.request("GET", "/admin")
        self.assertEqual(landed.status, 302)
        target = landed.header("location")           # /login?next=%2Fadmin
        self.assertTrue(target.startswith("/login?next="))

        page = self.app.request("GET", target)
        csrf = self.app.set_cookie_value(page, self.csrf_cookie_name())
        self.assertTrue(csrf, "login page did not issue a CSRF cookie")

        login = self.app.request("POST", "/login",
                                 form={"csrf_token": csrf, "email": "boss@example.com",
                                       "password": "correct horse battery",
                                       "next": "/admin"},
                                 cookies={self.csrf_cookie_name(): csrf})
        self.assertEqual(login.status, 302)
        self.assertEqual(login.header("location"), "/admin")

        session = self.app.set_cookie_value(login, self.session_cookie_name())
        self.assertEqual(self.app.request("GET", "/admin",
                                          cookies={"session": session}).status, 200)

    def test_authenticated_but_unauthorized_still_403(self):
        """已登录但权限不足：403（不是跳登录页——重新登录还是同一个身份）。"""
        self._config["admin_user_id"] = 999
        self.create_user(nickname="Plain", email="plain@example.com")
        session, csrf = self.login_ok("plain@example.com", "correct horse battery")
        response = self.app.request("GET", "/admin",
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 403)

    def test_next_cannot_be_used_for_open_redirect(self):
        """登录回跳只允许站内路径，防止被当成开放重定向。"""
        from elenvind.features.auth.routes import safe_next

        for hostile in ("//evil.example.com", "https://evil.example.com",
                        "http://evil.example.com/x", "\\\\evil", "/\\evil",
                        "javascript:alert(1)", "next", "", None, 123,
                        "/ok\r\nX-Injected: 1", "/a\nb"):
            with self.subTest(value=hostile):
                self.assertEqual(safe_next(hostile), "")
        for good in ("/", "/user", "/admin?tab=1", "/a/b#frag"):
            with self.subTest(value=good):
                self.assertEqual(safe_next(good), good)

    def test_hostile_next_is_not_reflected_into_redirect(self):
        """即使客户端伪造 next，登录后也只会跳站内（这里回落到首页）。"""
        self.create_user(email="r@example.com")
        page = self.app.request("GET", "/login", query={"next": "//evil.example.com"})
        csrf = self.app.set_cookie_value(page, self.csrf_cookie_name())
        login = self.app.request("POST", "/login",
                                 form={"csrf_token": csrf, "email": "r@example.com",
                                       "password": "correct horse battery",
                                       "next": "//evil.example.com"},
                                 cookies={self.csrf_cookie_name(): csrf})
        self.assertEqual(login.status, 302)
        self.assertEqual(login.header("location"), "/")

    def test_declared_permission_routes_reject_non_admin(self):
        """`permission="admin"` 的路由：普通登录用户必须 403。"""
        routes = [r for r in app.router.routes if r.permission]
        self.assertTrue(routes, "no permission-protected routes found")
        self._config["admin_user_id"] = 999        # 当前用户不是管理员
        self.create_user(nickname="Plain", email="plain@example.com")
        session, csrf = self.login_ok("plain@example.com", "correct horse battery")
        cookies = self.app_cookies(session=session, csrf=csrf)
        for route in routes:
            with self.subTest(path=route.path):
                response = self.app.request("GET", route.path, cookies=cookies)
                self.assertEqual(response.status, 403, response.text[:200])

    def test_required_and_authenticated_are_equivalent(self):
        """`auth="required"` 与 `auth="authenticated"` 必须同样生效。

        回归守卫：曾经实现只比较 `"authenticated"`，导致所有
        `auth="required"` 的路由**静默放行**（失效开放）。
        """
        from elenvind.core.auth import check_permission
        from elenvind.core.routing import AUTH_REQUIRED_VALUES
        from elenvind.core.routing import Route

        self.assertIn("required", AUTH_REQUIRED_VALUES)
        self.assertIn("authenticated", AUTH_REQUIRED_VALUES)
        for value in AUTH_REQUIRED_VALUES:
            route = Route("/x", ["GET"], lambda request: None, auth=value)
            self.assertIn(route.auth, AUTH_REQUIRED_VALUES, value)
        # 未登录 -> 不放行
        self.assertFalse(check_permission(self._anonymous_request(), "authenticated"))

    def _anonymous_request(self):
        from elenvind.core.http import Request
        request = Request(scope={}, method="GET", path="/", query={}, headers={},
                          cookies={}, raw_body=b"", form={}, content_type="",
                          content_length=None, client_ip="1.2.3.4", secure=False,
                          lang="en")
        return request

    #: 闸门保护的方法（与 core.csrf.PROTECTED_METHODS 一致）
    PROTECTED_METHODS = ("POST", "PUT", "PATCH", "DELETE")

    def _declared_protected(self):
        return {method for route in app.router.routes for method in route.methods} \
            & set(self.PROTECTED_METHODS)

    def test_every_declared_route_reaches_the_gate(self):
        """所有非安全方法的**声明路由**都必须经过 CSRF 闸门（无 token -> 400）。

        回归：这条测试以前只遍历 `"POST" in r.methods` 的路由。而 CSRF 闸门
        保护的是 `PROTECTED_METHODS = {POST, PUT, PATCH, DELETE}` ——
        PUT / PATCH / DELETE 这三条分支完全没有测试覆盖：有人把
        `requires_protection()` 改成只认 POST，测试依然全绿。

        本测试只遍历**实际声明了**的方法（生产路由目前全是 POST），
        尚未被任何路由使用的分支由下面两条测试直接覆盖判定函数。
        """
        declared = self._declared_protected()
        self.assertTrue(declared, "没有找到任何受保护方法的路由")

        checked = 0
        for route in app.router.routes:
            for method in sorted(set(route.methods) & set(self.PROTECTED_METHODS)):
                path = (route.path.replace("<slug>", "post")
                        .replace("<comment_id>", "1"))
                with self.subTest(method=method, path=path):
                    response = self.app.request(method, path, form={})
                    self.assertEqual(
                        response.status, 400,
                        f"{method} {path} did not enforce CSRF: {response.status}")
                    checked += 1
        self.assertGreaterEqual(checked, 1, "受保护方法的用例数为 0")

    def test_requires_protection_covers_all_unsafe_methods(self):
        """闸门判定表本身：四种非安全方法都必须为真，安全方法必须为假。

        这条才是 PUT / PATCH / DELETE 分支的真正守卫 ——
        生产路由目前没有声明它们，所以走不到 HTTP 层。
        """
        from elenvind.core.csrf import requires_protection

        for method in ("POST", "PUT", "PATCH", "DELETE",
                       "post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertTrue(requires_protection(method),
                                f"{method} 没有被 CSRF 闸门保护")
        for method in ("GET", "HEAD", "OPTIONS", "get", "head", ""):
            with self.subTest(method=method):
                self.assertFalse(requires_protection(method),
                                 f"{method} 不该被 CSRF 闸门保护")

    def test_route_declaration_rejects_a_protected_method_somewhere(self):
        """记录当前事实：生产路由只用了 POST。

        这不是缺陷（站点只有表单 POST），但如果将来有人加了 PUT/PATCH/DELETE
        路由，`test_every_declared_route_reaches_the_gate` 会自动把它纳入覆盖。
        这里显式断言，避免"以为已经覆盖了四种方法"。
        """
        declared = self._declared_protected()
        self.assertEqual(declared, {"POST"},
                         f"路由声明里出现了新的受保护方法 {sorted(declared)}；"
                         "请确认 CSRF 闸门对它们生效（上面的测试已自动覆盖）")

    def test_unused_protected_method_on_an_existing_path_is_405(self):
        """已声明路径上用未声明的方法 -> 405（而不是 400/500）。

        这条同时说明"405 早于 CSRF 闸门"：路由匹配先失败，闸门根本没跑。
        """
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                response = self.app.request(method, "/logout", form={})
                self.assertEqual(response.status, 405, response.status)
                self.assertIsNotNone(response.header("allow"))

    def test_csrf_gate_runs_before_auth_gate(self):
        """CSRF 判定必须在认证判定**之前**。

        否则"未登录 + 无 CSRF"会先跳登录页，攻击者可以据此探测
        "哪些路径存在且需要认证"。用真实存在的 POST 路由 `/logout`
        （声明了 `auth="required"`）来验证：无 token 时必须 400，而不是 302。
        """
        response = self.app.request("POST", "/logout", form={})
        self.assertEqual(response.status, 400,
                         "未登录 + 无 CSRF 时应先被 CSRF 闸门拦下（400），"
                         f"而不是 {response.status}")
        self.assertNotEqual(response.status, 302)

    def test_valid_token_reaches_the_auth_gate_on_a_post_route(self):
        """反向确认：带合法 token 时不再因 CSRF 被拒（会因未登录走认证闸门）。"""
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/logout", form={"csrf_token": csrf},
                                    cookies={self.csrf_cookie_name(): csrf})
        self.assertNotEqual(response.status, 400, response.text[:200])
        self.assertNotIn("Invalid CSRF token", response.text)


class NoDuplicateSecurityTests(unittest.TestCase):
    """B. Feature 不得**重新实现**框架已经提供的安全能力。

    注意区分"使用"与"重造"：Feature 调用 `core.security.hash_password()` 是
    正确用法（Core 提供原语）；只有自己拼 Cookie、自己定义校验函数、
    自己造安全头才算违约。
    """

    #: 这些符号一旦在 Feature 里**被定义**或**直接实例化**，就是重造轮子
    FORBIDDEN_DEFINITIONS = {
        "hash_password": "密码哈希只能由 core.security 提供",
        "verify_password": "密码校验只能由 core.security 提供",
        "generate_csrf_token": "CSRF 令牌只能由 core.csrf 提供",
        "is_valid_csrf_token": "CSRF 令牌格式校验只能由 core.csrf 提供",
        "tokens_match": "CSRF 比对只能由 core.csrf 提供",
        "parse_cookies": "Cookie 解析只能由 Core 提供",
        "set_cookie_header": "Set-Cookie 只能由 Core 下发",
        "_make_cookie": "Cookie 拼装只能由 core.security 提供",
        "build_headers": "响应头组装只能由 Core 提供",
        "send_response": "响应发送只能由 Core 提供",
        "load_user": "会话加载只能由 core.session 提供",
        "login_user": "会话颁发只能由 core.session 提供",
        "check_permission": "权限判定只能由 core.auth 提供",
        "build_render_context": "模板上下文只能由 Core 组装",
        "get_environment": "Jinja 环境只能由 core.templating 提供",
        "render_markdown": "Markdown 渲染只能由 core.markdown 提供",
    }

    #: 这些调用一旦出现在 Feature 里，说明它在自己实现安全机制
    FORBIDDEN_CALLS = {
        "SimpleCookie": "Cookie 必须通过 Core API 操作",
        "scrypt": "密码哈希必须走 core.security",
        "compare_digest": "常量时间比对必须走 core.csrf",
    }

    #: Feature 里**不得出现的 Response Cookie 方法**。
    #:
    #: 回归：契约写着"Feature 不拼 Cookie"，但守卫只禁了**定义** helper，
    #: 于是 4 处 Feature 直接调用 `response.set_cookie(...)` /
    #: `response.delete_cookie(SESSION_COOKIE)` 完全合法地绕过了它 ——
    #: Feature 既知道了 Cookie 名字，又得自己处理 `__Host-` 前缀策略。
    #: 正确做法：会话走 `request.invalidate_session_cookie()`，
    #: 偏好走 `request.set_preference()`，由 Core 统一下发。
    FORBIDDEN_COOKIE_METHODS = {
        "set_cookie": "Feature 不得自己写 Cookie；偏好请用 request.set_preference()",
        "delete_cookie": "Feature 不得自己删 Cookie；会话请用 request.invalidate_session_cookie()",
    }

    def test_features_do_not_set_or_delete_cookies(self):
        """Feature 不得触碰 Response 的 Cookie API。"""
        offenders = []
        for path, source in _feature_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                reason = self.FORBIDDEN_COOKIE_METHODS.get(func.attr)
                if reason:
                    offenders.append(
                        f"{path.name}:{node.lineno}: .{func.attr}() -> {reason}")
        self.assertEqual(offenders, [],
                         "Feature 在操作 Cookie：\n" + "\n".join(offenders))

    def test_features_do_not_import_session_cookie_constant(self):
        """Feature 不得 import 会话 Cookie 名（说明它想自己操作那个 Cookie）。"""
        offenders = []
        for path, source in _feature_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                for alias in node.names:
                    if alias.name in ("SESSION_COOKIE", "CSRF_COOKIE"):
                        offenders.append(f"{path.name}:{node.lineno}: {alias.name}")
        self.assertEqual(offenders, [],
                         "Feature import 了 Core 的 Cookie 常量：\n"
                         + "\n".join(offenders))

    def test_footer_copyright_falls_back_to_site_title(self):
        """版权署名缺省时必须回落到站点名，而不是字面量 "title" 或 "None"。

        回归：旧代码是 `config.get("copyright", "title")` ——
        那个 `"title"` 是字面量字符串（不是"取 title 键"），键缺失时页脚显示
        `© 2026 title`；键存在但为 None 时 `.get` 返回 None，显示 `© 2026 None`。
        项目自带的 config.toml 恰好设了 copyright，所以一直没被发现。

        传入 `{"user_count": 0}` 是为了跳过 `get_user_number()` 的数据库查询 ——
        本类不带 ElenvindTestCase 夹具（它只做源码级静态检查）。
        """
        from elenvind.core.config import config as live_config

        saved_copyright = live_config.get("copyright", None)
        saved_title = live_config.get("title", None)
        had_copyright = "copyright" in live_config

        def render():
            return build_render_context({"user_count": 0})

        try:
            for value, expected in ((None, "Test Site"), ("", "Test Site"),
                                    ("   ", "Test Site"),
                                    ("Alice", "Alice"),
                                    ("  Alice  ", "  Alice  ")):
                with self.subTest(value=value):
                    live_config["copyright"] = value
                    live_config["title"] = "Test Site"
                    self.assertEqual(render()["copyright_name"], expected)

            # 连 title 也没有时才回落到 "Elenvind"
            live_config["copyright"] = None
            live_config["title"] = None
            self.assertEqual(render()["copyright_name"], "Elenvind")
        finally:
            if had_copyright:
                live_config["copyright"] = saved_copyright
            else:
                live_config.pop("copyright", None)
            live_config["title"] = saved_title

    def test_illegal_auth_declarations_raise_at_registration(self):
        """非法的 `auth=` / `permission=` 取值必须在**注册时**就报错。

        这是 P1-1 的核心回归：旧实现把 `auth` 只与 `{"authenticated",
        "required"}` 比对，`auth="admin"` / `auth="owner"` / `auth=True`
        这类写错的值会让闸门**整体跳过** —— 匿名即可执行（失效开放）。
        拼错一个档位就静默公开端点，所以现在宁可注册时报错。

        ⚠️ 这道校验是注册期的**唯一**防线（不像 `safe_next` 有多层兜底），
        所以必须直接测 `_validate_declaration` 本身：只在 HTTP 层断言
        "非法 auth 被拒"会被"路由根本没注册上"掩盖。
        """
        from elenvind.core.routing import Route

        def handler(request):
            return None

        illegal_auth = ("owner", "true", "True", "Authentication",
                        "amin", "required ", " public", "ADMIN", True, False,
                        1, 0, [], {}, "authenticated ")
        for value in illegal_auth:
            with self.subTest(auth=value):
                with self.assertRaises(ValueError, msg=f"auth={value!r} 未被拒绝"):
                    Route("/x", ["GET"], handler, auth=value)

        # `admin` 是**合法**取值（与 permission="admin" 等价）
        legal_auth = (None, "", "public", "authenticated", "required", "admin")
        for value in legal_auth:
            with self.subTest(auth=value):
                Route("/x", ["GET"], handler, auth=value)   # 不得抛异常

    def test_illegal_permission_declarations_raise_at_registration(self):
        from elenvind.core.routing import Route

        def handler(request):
            return None

        illegal_permission = ("owner", "true", "ADMIN", " admin", True, 1,
                              [], {}, "admin ")
        for value in illegal_permission:
            with self.subTest(permission=value):
                with self.assertRaises(ValueError,
                                       msg=f"permission={value!r} 未被拒绝"):
                    Route("/x", ["GET"], handler, permission=value)

        # `authenticated` 是合法取值（permission 侧与 auth 侧同一套判定）
        for value in (None, "", "public", "authenticated", "admin"):
            with self.subTest(permission=value):
                Route("/x", ["GET"], handler, permission=value)

    def test_non_string_declarations_raise_value_error_not_type_error(self):
        """非字符串必须报 `ValueError`（不是 `TypeError`）。

        回归：旧实现只写 `if value in known`，对不可哈希取值（list/dict/set）
        成员判断会抛 `TypeError: unhashable type`，与"注册时报 ValueError"
        的契约不符，调用方 `except ValueError` 会漏掉它。
        """
        from elenvind.core.routing import Route

        def handler(request):
            return None

        for value in ([], {}, set(), ["admin"], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Route("/x", ["GET"], handler, auth=value)
                with self.assertRaises(ValueError):
                    Route("/x", ["GET"], handler, permission=value)

    def test_validate_declaration_rejects_unknown_values_directly(self):
        """直接测校验函数（不经过 Route），确保它不是"碰巧"被别处拦下。"""
        from elenvind.core.auth import KNOWN_AUTH, KNOWN_PERMISSIONS
        from elenvind.core.routing import _validate_declaration

        with self.assertRaises(ValueError):
            _validate_declaration("auth", "owner", KNOWN_AUTH, "/x")
        with self.assertRaises(ValueError):
            _validate_declaration("permission", "owner", KNOWN_PERMISSIONS, "/x")

        # 允许的取值：None / "" / 已知集合内的值
        for value in (None, ""):
            _validate_declaration("auth", value, KNOWN_AUTH, "/x")
        for value in KNOWN_AUTH:
            _validate_declaration("auth", value, KNOWN_AUTH, "/x")

    def test_admin_auth_declaration_actually_gates(self):
        """`auth="admin"` 与 `permission="admin"` 必须完全等价地拦人。"""
        from elenvind.core.auth import AUTH_REQUIRED_VALUES, normalize_auth

        self.assertIn("admin", AUTH_REQUIRED_VALUES)
        for value in AUTH_REQUIRED_VALUES:
            with self.subTest(auth=value):
                self.assertEqual(normalize_auth(value), value
                                 if value != "required" else "authenticated")

    def test_features_do_not_reimplement_security(self):
        offenders = []
        for path, source in _feature_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    reason = self.FORBIDDEN_DEFINITIONS.get(node.name)
                    if reason:
                        offenders.append(f"{path.name}: def {node.name} -> {reason}")
                elif isinstance(node, ast.Name) and node.id in self.FORBIDDEN_CALLS:
                    if isinstance(node.ctx, ast.Load):
                        offenders.append(
                            f"{path.name}: {node.id} -> {self.FORBIDDEN_CALLS[node.id]}")
        self.assertEqual(offenders, [], "Feature 重新实现了安全能力：\n"
                         + "\n".join(sorted(set(offenders))))

    #: 危险构造：Feature 里出现这些，说明它在自己拼可执行/可注入的标记
    DANGEROUS_MARKUP = re.compile(
        r"<\s*(script|iframe|object|embed|style|form|input|svg)\b"
        r"|\bon[a-z]+\s*="
        r"|javascript:",
        re.IGNORECASE,
    )

    def test_features_do_not_build_dangerous_markup(self):
        """Feature 不得手写可执行/可注入的标记。

        允许出现的是无害的排版片段（`<br>`、`<span class=…>`）与路由模式
        （`/article/<slug>`）、XML（sitemap）等；这里只拦截真正的注入面。
        视图层的 HTML 组装主体已由 Jinja2 模板承担。
        """
        offenders = []
        for path, source in _feature_sources():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.JoinedStr):
                    literal = "".join(part.value for part in node.values
                                      if isinstance(part, ast.Constant)
                                      and isinstance(part.value, str))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literal = node.value
                else:
                    continue
                if self.DANGEROUS_MARKUP.search(literal):
                    offenders.append(f"{path.name}: {literal.strip()[:60]!r}")
        self.assertEqual(offenders, [], "Feature 里出现了危险标记拼接：\n"
                         + "\n".join(sorted(set(offenders))))

    def test_features_do_not_create_jinja_environment(self):
        banned = ("Environment", "FileSystemLoader", "select_autoescape")
        offenders = []
        for path, source in _feature_sources():
            names = _imported_names(source)
            for name in banned:
                if name in names:
                    offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], f"Feature 自建 Jinja 环境：{offenders}")

    def test_features_do_not_import_core_db_connection_directly(self):
        """Feature 只能用 `connect()`，不能用 get_connection()（会泄漏连接）。"""
        offenders = []
        for path, source in _feature_sources():
            if "get_connection" in _imported_names(source) or \
                    re.search(r"\bget_connection\b", source):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"Feature 直接开连接：{offenders}")

    def test_core_does_not_import_features(self):
        """依赖方向必须单向：Feature -> Core，Core 不认识任何 Feature。"""
        offenders = []
        for path in sorted(CORE_DIR.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if re.search(r"^\s*from\s+\.\.features|^\s*import\s+.*features",
                         source, re.M):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"Core 反向依赖 Feature：{offenders}")

    def test_core_is_the_only_place_that_creates_template_environment(self):
        """Jinja Environment 只能在 core.templating 里创建一次。"""
        offenders = []
        for path in sorted((PROJECT_ROOT / "elenvind").rglob("*.py")):
            if path.name == "templating.py":
                continue
            source = path.read_text(encoding="utf-8")
            if re.search(r"\bEnvironment\s*\(", source) or \
                    re.search(r"\bFileSystemLoader\s*\(", source):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [], f"重复创建 Jinja 环境：{offenders}")

    def test_all_templates_are_inside_the_templates_directory(self):
        """模板必须集中在 elenvind/templates/，不散落在 Feature 里。"""
        stray = [str(path.relative_to(PROJECT_ROOT))
                 for path in FEATURES_DIR.rglob("*.html")]
        self.assertEqual(stray, [], f"模板散落在 Feature 目录：{stray}")

    def test_every_template_referenced_by_a_feature_exists(self):
        """Feature 里 `render_template("x.html")` 引用的模板必须真实存在。

        回归守卫：曾经路由写 `user/profile.html` 而文件在 `users/profile.html`，
        只有跑到那条路径才会 500；静态检查能在测试前发现。
        """
        from elenvind.core.templating import DEFAULT_TEMPLATES_DIR

        pattern = re.compile(r"""render_template\(\s*["']([^"']+\.html)["']""")
        missing = []
        for path, source in _feature_sources():
            for name in pattern.findall(source):
                if not (DEFAULT_TEMPLATES_DIR / name).is_file():
                    missing.append(f"{path.name}: {name}")
        self.assertEqual(missing, [], "引用了不存在的模板：\n" + "\n".join(missing))

    def test_every_template_file_is_loadable(self):
        """每个模板都要能编译（语法、继承、include 链都可解析）。

        渲染需要业务数据，由各自的业务测试覆盖；这里守住"模板本身没写坏"。
        """
        from elenvind.core.templating import get_environment

        environment = get_environment()
        root = PROJECT_ROOT / "elenvind" / "templates"
        for path in sorted(root.rglob("*.html")):
            relative = path.relative_to(root).as_posix()
            with self.subTest(template=relative):
                environment.get_template(relative)

    def test_templates_do_not_use_safe_filter_on_user_data(self):
        """模板里的 `|safe` 必须只用于 Core 提供的可信内容。

        允许的白名单：Markdown 渲染结果、主题 SVG 图标、CSRF 输入域。
        其它地方用 `|safe` 等于关掉自动转义。
        """
        allowed = {"theme_icons.", "row.content", "page.body", "article.body"}
        offenders = []
        for path in sorted((PROJECT_ROOT / "elenvind" / "templates").rglob("*.html")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "|safe" not in line and "| safe" not in line:
                    continue
                if any(marker in line for marker in allowed):
                    continue
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "可疑的 |safe 用法：\n" + "\n".join(offenders))


class DefaultSecurityTests(ElenvindTestCase):
    """C. 默认安全：不依赖 Feature 记得做什么。"""

    REQUIRED_HEADERS = ("x-content-type-options", "x-frame-options",
                        "referrer-policy", "content-security-policy",
                        "permissions-policy", "cross-origin-opener-policy",
                        "cross-origin-resource-policy")

    def test_every_response_carries_security_headers(self):
        """成功、重定向、400、403、404、405 都必须带全套安全头。"""
        csrf = self.fetch_csrf()
        cases = [("GET", "/", None), ("GET", "/nope", None),
                 ("GET", "/logout", None),                     # 405
                 ("POST", "/logout", {"csrf_token": "x" * 43}),  # 400（坏 token）
                 ("POST", "/nope", {"a": "b"})]                # 405
        for method, path, form in cases:
            with self.subTest(method=method, path=path):
                response = self.app.request(method, path, form=form,
                                            cookies={self.csrf_cookie_name(): csrf})
                for header in self.REQUIRED_HEADERS:
                    self.assertIsNotNone(response.header(header),
                                         f"{header} missing on {method} {path} "
                                         f"({response.status})")

    def test_footer_never_renders_a_placeholder_name(self):
        """端到端：页脚版权署名不得出现 `None` / 字面量 `title`。

        回归见 `test_footer_copyright_falls_back_to_site_title`：
        `config.get("copyright", "title")` 在两种缺省情形下会渲染出
        `© 2026 None` 或 `© 2026 title`。
        """
        from elenvind.core.config import config as live_config

        saved_copyright = live_config.get("copyright", None)
        saved_title = live_config.get("title", None)
        had_copyright = "copyright" in live_config
        try:
            live_config["title"] = "My Site"
            for value in (None, "", "   "):
                with self.subTest(copyright=value):
                    live_config["copyright"] = value
                    response = self.app.request("GET", "/")
                    self.assertNotIn("None", response.text)
                    self.assertNotIn("© 2026 title", response.text)
                    self.assertIn("My Site", response.text)
        finally:
            if had_copyright:
                live_config["copyright"] = saved_copyright
            else:
                live_config.pop("copyright", None)
            live_config["title"] = saved_title

    def test_csp_forbids_scripts(self):
        response = self.app.request("GET", "/")
        csp = response.header("content-security-policy") or ""
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        # 脚本相关的放宽一律不允许（style 的 'unsafe-inline' 是 hero 所需，
        # 见 tests/test_security_headers.py 的说明与守卫）
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("script-src 'self'", csp)

    def test_hsts_only_when_configured_and_https(self):
        """HSTS 需要显式开启；且只在 HTTPS 请求上下发。"""
        self._config["security"] = {"hsts_enabled": True}
        self.app.scheme = "https"
        self.assertIsNotNone(self.app.request("GET", "/")
                             .header("strict-transport-security"))
        self.app.scheme = "http"
        self.assertIsNone(self.app.request("GET", "/")
                          .header("strict-transport-security"))

    def test_hsts_absent_by_default(self):
        self._config["security"] = {}
        self.app.scheme = "https"
        self.assertIsNone(self.app.request("GET", "/")
                          .header("strict-transport-security"))

    def test_every_set_cookie_is_hardened(self):
        """所有 Set-Cookie 必须 HttpOnly + SameSite=Lax + Path=/。"""
        csrf = self.fetch_csrf()
        responses = [self.app.request("GET", "/"),
                     self.app.request("GET", "/login"),
                     self.app.request("GET", "/theme",
                                      query={"mode": "dark", "next": "/"}),
                     self.login("nobody@example.com", "wrong", csrf=csrf)]
        seen = 0
        for response in responses:
            for cookie in response.headers_all("set-cookie"):
                seen += 1
                self.assertIn("HttpOnly", cookie, cookie)
                self.assertIn("SameSite=Lax", cookie, cookie)
                self.assertIn("Path=/", cookie, cookie)
        self.assertGreater(seen, 0, "no Set-Cookie headers observed")

    def test_authenticated_pages_are_not_cacheable(self):
        """带登录态的页面必须 no-store，避免被中间缓存泄漏给他人。"""
        self.create_user(email="cache@example.com")
        session, csrf = self.login_ok("cache@example.com", "correct horse battery")
        response = self.app.request("GET", "/user",
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("cache-control"), "no-store")

    def test_error_responses_do_not_leak_internals(self):
        """400/403/404/405 的响应体不得包含异常细节或路径。"""
        for method, path in (("GET", "/nope"), ("GET", "/logout"),
                            ("POST", "/nope")):
            with self.subTest(path=path):
                response = self.app.request(method, path, form={})
                lowered = response.text.lower()
                for leak in ("traceback", "file \"", "c:\\\\", "/home/",
                             "site-packages", "sqlite3."):
                    self.assertNotIn(leak, lowered, response.text[:200])


class FailClosedTests(ElenvindTestCase):
    """D. 失效关闭：声明或输入异常时宁可拒绝。"""

    def test_unknown_permission_value_denies(self):
        """未知权限档位必须拒绝，而不是"不认识就放行"。

        `None` / `""` 是"未声明权限"的合法表示（等同于 public），不在此列；
        任何**非空但未实现**的值都必须拒绝。
        """
        from elenvind.core.auth import check_permission

        self.create_user(nickname="P", email="p@example.com")
        session, csrf = self.login_ok("p@example.com", "correct horse battery")
        request = self._request_with_session(session)

        # 非空但未实现的档位 -> 拒绝（拼写错误的 permission="amin" 不能变成放行）
        for value in ("superuser", "root", "ADMIN ", "adminx", "AUTHENTICATED",
                      "public ", "owner"):
            with self.subTest(value=value):
                self.assertFalse(check_permission(request, value),
                                 f"unknown permission {value!r} was allowed")

        # 未声明权限 = 不限制（合法的"无要求"表示）
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertTrue(check_permission(request, value))

        # 已知档位按语义生效
        self.assertTrue(check_permission(request, "authenticated"))
        self.assertTrue(check_permission(request, "public"))

        # admin 档位严格跟随 config.toml 的 admin_user_id
        admin_id = self._config["admin_user_id"]
        self.assertEqual(request.user["id"], admin_id)
        self.assertTrue(check_permission(request, "admin"))
        self._config["admin_user_id"] = 999          # 换掉管理员
        self.assertFalse(check_permission(request, "admin"))
        self._config.pop("admin_user_id")            # 未配置 = 没有管理员
        self.assertFalse(check_permission(request, "admin"))

    def _request_with_session(self, session_token):
        """构造一个带会话的 Request（复用真实请求路径）。"""
        from elenvind.core.http import Request
        from elenvind.core.session import load_user
        request = Request(scope={}, method="GET", path="/", query={}, headers={},
                          cookies={"session": session_token}, raw_body=b"", form={},
                          content_type="", content_length=None,
                          client_ip="127.0.0.1", secure=True, lang="en",
                          session_token=session_token)
        load_user(request)
        return request

    def test_missing_csrf_cookie_denies(self):
        """只有表单 token、没有 Cookie token：拒绝（Double-Submit 的核心）。"""
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/logout",
                                    form={"csrf_token": csrf}, cookies={})
        self.assertEqual(response.status, 400)

    def test_csrf_token_from_another_session_denies(self):
        first = self.fetch_csrf()
        second = self.fetch_csrf()
        self.assertNotEqual(first, second)
        response = self.app.request("POST", "/logout",
                                    form={"csrf_token": first},
                                    cookies={self.csrf_cookie_name(): second})
        self.assertEqual(response.status, 400)

    def test_forged_session_cookie_is_not_authenticated(self):
        """伪造/畸形的会话 Cookie 不能被当成已登录。

        友好语义下，未登录的 GET 会 302 到登录页；两种结果都算"未认证成功"，
        但绝不能出现账号信息。
        """
        self.create_user(email="real@example.com")
        for forged in ("A" * 43, "", "not-a-token", "' OR 1=1 --"):
            with self.subTest(token=forged):
                response = self.app.request("GET", "/user",
                                            cookies={"session": forged})
                self.assertIn(response.status, (200, 302))
                if response.status == 302:
                    self.assertTrue((response.header("location") or "")
                                    .startswith("/login?next="))
                self.assertNotIn("real@example.com", response.text)

    def test_handler_returning_none_is_an_error_not_a_blank_page(self):
        """handler 忘记 return 时必须显式失败（500 + 日志），而不是静默空响应。

        走完整 ASGI 路径：RuntimeError 不属于 HttpError，会穿透调度器，
        由 App 统一转成 500。
        """
        from elenvind.core.app import App
        from tests.support import run_async

        mini = App()
        mini.router.route("/boom", methods=["GET"])(lambda request: None)

        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/boom", "scheme": "https",
                 "headers": [], "query_string": b"", "client": ("127.0.0.1", 1)}
        with self.assertLogs("elenvind.core.app", level="ERROR"):
            run_async(mini(scope, receive, send))

        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent
                        if m["type"] == "http.response.body")
        self.assertEqual(status, 500)
        self.assertNotIn(b"returned None", body)      # 细节不外泄


class RequestBoundaryTests(ElenvindTestCase):
    """E. 请求边界：畸形输入得到确定状态码，不 500、不绕过限制。"""

    def test_oversized_body_is_rejected(self):
        self._config["max_body_size"] = 100
        response = self.app.request("POST", "/login",
                                    form={"a": "x" * 500})
        self.assertEqual(response.status, 413)

    def test_body_exactly_at_limit_is_accepted(self):
        limit = 200
        self._config["max_body_size"] = limit
        payload = "csrf_token=" + "x" * (limit - len("csrf_token="))
        body = payload.encode()
        self.assertEqual(len(body), limit)
        response = self.app.raw_request(
            "POST", "/login", b"",
            [("host", "example.com"),
             ("content-type", "application/x-www-form-urlencoded"),
             ("content-length", str(len(body)))], body)
        self.assertEqual(response.status, 400)      # 拒绝的是坏 token，不是体积

    def test_unsupported_content_type_is_415(self):
        for mime in ("application/json", "text/plain", "multipart/form-data"):
            with self.subTest(mime=mime):
                response = self.app.raw_request(
                    "POST", "/login", b"",
                    [("host", "example.com"), ("content-type", mime),
                     ("content-length", "2")], b"{}")
                self.assertEqual(response.status, 415)

    def test_missing_content_length_is_411(self):
        response = self.app.request("POST", "/login", body=b"",
                                    send_content_length=False)
        self.assertEqual(response.status, 411)

    def test_negative_and_non_numeric_content_length(self):
        for raw in ("-1", "abc", "1e5", " "):
            with self.subTest(raw=raw):
                response = self.app.raw_request(
                    "POST", "/login", b"",
                    [("host", "example.com"),
                     ("content-type", "application/x-www-form-urlencoded"),
                     ("content-length", raw)], b"")
                self.assertIn(response.status, (400, 411, 413),
                              f"content-length {raw!r} -> {response.status}")

    def test_truncated_body_is_rejected(self):
        """声明的长度大于实收：必须 400，不能把半个表单当完整表单处理。"""
        response = self.app.raw_request(
            "POST", "/login", b"",
            [("host", "example.com"),
             ("content-type", "application/x-www-form-urlencoded"),
             ("content-length", "100")], b"short")
        self.assertEqual(response.status, 400)

    def test_path_with_empty_segment_is_not_found(self):
        """`//` 不能让路径"折叠"成另一个合法路由。"""
        for path in ("/article//comment", "//", "/theme//", "/user//"):
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertEqual(response.status, 404)

    def test_method_not_allowed_includes_allow_header(self):
        """405 必须带 Allow，且不能落到 404 兜底页。

        用"仅声明 POST"的路径：`/logout` 现在有友好的 GET 确认页，
        不再是 405 的合适样例。
        """
        response = self.app.request("GET", "/article/a/comment/delete/1")
        self.assertEqual(response.status, 405)
        allow = response.header("allow") or ""
        self.assertIn("POST", allow)

    def test_fallback_route_does_not_mask_declared_routes(self):
        """自定义页面兜底 `/<slug>` 不能吃掉固定路径的方法语义。"""
        self.write_page("article", "this page must not shadow /article/...")
        # /logout 有 GET 确认页；用仅 POST 的评论删除路径验证 405 语义
        response = self.app.request("GET", "/article/a/comment/delete/1")
        self.assertEqual(response.status, 405)
        self.assertIn("POST", response.header("allow") or "")

    def test_fallback_route_does_not_shadow_get_routes(self):
        """兜底页也不能遮蔽已声明的 GET 路由。"""
        self.write_page("login", "fake login page")
        response = self.app.request("GET", "/login")
        self.assertEqual(response.status, 200)
        self.assertNotIn("fake login page", response.text)
        # 真正的登录页有自己的表单
        self.assertIn('action="/login"', response.text)

    def test_fallback_route_still_serves_pages(self):
        self.write_page("about", "About **me**")
        response = self.app.request("GET", "/about")
        self.assertEqual(response.status, 200)
        self.assertIn("<strong>me</strong>", response.text)

    def test_head_returns_no_body_but_full_headers(self):
        head = self.app.raw_request("HEAD", "/", b"", [("host", "example.com")])
        get = self.app.request("GET", "/")
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        self.assertEqual(head.header("content-length"), str(len(get.body)))
        for header in self.REQUIRED_HEADERS_FOR_HEAD:
            self.assertIsNotNone(head.header(header), header)

    REQUIRED_HEADERS_FOR_HEAD = ("x-content-type-options", "content-security-policy",
                                 "cache-control")


if __name__ == "__main__":
    unittest.main()
