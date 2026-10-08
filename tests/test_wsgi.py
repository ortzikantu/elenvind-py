"""WSGI 契约测试：Elenvind 是同步 WSGI 应用，且不可能悄悄退回异步 runtime。

三层覆盖：

A. 入口契约   —— `elenvind.app.app` / `elenvind.wsgi:application` 是同步 callable，
                 且生产入口在导入时先完成 startup()；
B. environ 语义 —— PEP 3333 的请求头/请求体约定、SCRIPT_NAME 拼接、
                 转发头（X-Forwarded-Proto）的信任边界；
C. 静态守卫   —— 生产代码里不得再出现 async/await（异步 runtime 的结构形态），
                 也不得残留任何 uvicorn / asgi 引用。
"""
import ast
import importlib
import inspect
import re
import sys
import unittest

from tests.support import PROJECT_ROOT, ElenvindTestCase, build_environ, call_wsgi

PACKAGE = PROJECT_ROOT / "elenvind"


def _production_sources():
    """生产代码（elenvind 包 + 仓库根的 run.py）。"""
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")
    run_py = PROJECT_ROOT / "run.py"
    yield run_py, run_py.read_text(encoding="utf-8")


class EntryPointContractTests(ElenvindTestCase):
    """A. 应用可调用对象就是 WSGI callable。"""

    def test_application_is_a_synchronous_wsgi_callable(self):
        from elenvind.app import app

        self.assertTrue(callable(app), "application 必须是 callable")
        self.assertFalse(inspect.iscoroutinefunction(app.__call__),
                         "WSGI callable 不能是协程函数")
        # WSGI 签名：(environ, start_response) —— 两个位置参数，无默认值
        parameters = list(inspect.signature(app.__call__).parameters.values())
        self.assertEqual([p.name for p in parameters],
                         ["environ", "start_response"])

    def test_production_entry_module_runs_startup_then_exposes_application(self):
        """`elenvind.wsgi`：导入时 startup(STARTUP_HOOKS) 一次，然后暴露 `application`。

        没有 lifespan 协议可依赖，所以"启动即导入"是唯一可靠的时机；
        这里把 startup/shutdown 换成探针，验证顺序、启动钩子的来源
        （装配层的 STARTUP_HOOKS）与暴露的符号，而不真的再去读一次 config.toml。
        """
        import elenvind.core.lifespan as lifespan_module

        recorded = []
        original_startup = lifespan_module.startup
        original_shutdown = lifespan_module.shutdown
        lifespan_module.startup = lambda hooks=(), prepare_database=None: recorded.append(tuple(hooks))
        lifespan_module.shutdown = lambda: recorded.append("shutdown")
        sys.modules.pop("elenvind.wsgi", None)
        try:
            module = importlib.import_module("elenvind.wsgi")
            self.assertEqual(len(recorded), 1,
                             "生产入口必须恰好调用一次 startup()")
            # 启动钩子必须来自装配层（Core 不再持有模块注册表）
            from elenvind.app import STARTUP_HOOKS, app
            self.assertEqual(recorded[0], tuple(STARTUP_HOOKS))
            self.assertEqual([label for label, _ in recorded[0]],
                             ["Articles loaded", "Custom pages loaded"])
            self.assertIs(module.application, app)
            self.assertTrue(callable(module.application))
        finally:
            lifespan_module.startup = original_startup
            lifespan_module.shutdown = original_shutdown
            sys.modules.pop("elenvind.wsgi", None)

    def test_status_line_has_a_reason_phrase(self):
        from elenvind.core.http import status_line

        self.assertEqual(status_line(200), "200 OK")
        self.assertEqual(status_line(404), "404 Not Found")
        self.assertEqual(status_line(500), "500 Internal Server Error")
        self.assertTrue(status_line(599).startswith("599"))

    def test_response_headers_are_native_str_and_latin1_safe(self):
        """PEP 3333：响应头必须是 native str 且可 latin-1 编码。

        服务器（gunicorn）会把非法类型直接判为协议错误，所以这条必须由
        应用侧保证 —— 这里直接收 `start_response` 的入参来验证。
        """
        from elenvind.app import app

        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            return lambda data: None

        body = b"".join(app(build_environ("GET", "/", headers=[("host", "example.com")]),
                            start_response))
        self.assertIsInstance(captured["status"], str)
        self.assertGreater(len(body), 0)
        for name, value in captured["headers"]:
            with self.subTest(header=name):
                self.assertIsInstance(name, str)
                self.assertIsInstance(value, str)
                name.encode("latin-1")
                value.encode("latin-1")


class EnvironSemanticsTests(ElenvindTestCase):
    """B. PEP 3333 的请求侧约定。"""

    def test_collect_headers_reads_wsgi_content_keys(self):
        """`CONTENT_TYPE` / `CONTENT_LENGTH` 是独立键，不带 `HTTP_` 前缀。"""
        from elenvind.core.http import collect_headers

        headers = collect_headers({
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": "12",
            "HTTP_HOST": "example.com",
            "HTTP_X_FORWARDED_FOR": "1.2.3.4",
            "wsgi.input": object(),
        })
        self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(headers["content-length"], "12")
        self.assertEqual(headers["host"], "example.com")
        self.assertEqual(headers["x-forwarded-for"], "1.2.3.4")
        self.assertNotIn("request-method", headers)
        self.assertNotIn("wsgi-input", headers)

    def test_script_name_is_joined_with_path_info(self):
        """挂在子路径下时（SCRIPT_NAME 非空）必须按完整路径路由。

        这里故意把整条 `/login` 放进 SCRIPT_NAME、PATH_INFO 留空：
        只看 PATH_INFO 会得到首页（200 但内容是首页），拼接后才命中登录页。
        """
        from elenvind.app import app

        environ = build_environ("GET", "", headers=[("host", "example.com")])
        environ["SCRIPT_NAME"] = "/login"
        response = call_wsgi(app, environ)
        self.assertEqual(response.status, 200)
        self.assertIn('action="/login"', response.text)

    def test_forwarded_proto_is_trusted_only_from_a_trusted_peer(self):
        """X-Forwarded-Proto 只在直连对端是受信代理时决定 https 判定。

        这是旧实现里由 HTTP 服务器（`forwarded_allow_ips`）与应用各判一次、
        容易配歪的地方；现在只有应用这一层，白名单是 `[server].trusted_proxies`。
        """
        self._config["security"] = {"hsts_enabled": True}
        self.app.scheme = "http"          # 服务器看到的明文连接
        forwarded = [("x-forwarded-proto", "https")]

        # 受信代理（默认 127.0.0.1）：采信 -> 视为 HTTPS -> 下发 HSTS
        self.app.client = ("127.0.0.1", 44321)
        trusted = self.app.request("GET", "/", extra_headers=forwarded)
        self.assertIsNotNone(trusted.header("strict-transport-security"))

        # 非可信直连：同一个头必须被忽略（否则直连者能骗到 Secure/HSTS 语义）
        self.app.client = ("198.51.100.7", 44321)
        untrusted = self.app.request("GET", "/", extra_headers=forwarded)
        self.assertIsNone(untrusted.header("strict-transport-security"))

    def test_forwarded_proto_never_downgrades_a_real_https_connection(self):
        """如果服务器自己就是 HTTPS（直连 TLS / PROXY protocol），仍然算安全。

        此时 X-Forwarded-Proto 无论写什么都不该把它降级成 http。
        """
        self._config["security"] = {"hsts_enabled": True}
        self.app.scheme = "https"
        self.app.client = ("198.51.100.7", 44321)
        response = self.app.request("GET", "/",
                                    extra_headers=[("x-forwarded-proto", "http")])
        self.assertIsNotNone(response.header("strict-transport-security"))


class NoAsyncRuntimeGuardTests(unittest.TestCase):
    """C. 静态守卫：异步 runtime 的结构与字样都不该回来。"""

    def test_production_code_has_no_async_constructs(self):
        offenders = []
        for path, source in _production_sources():
            tree = ast.parse(source, str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    offenders.append(f"{path.name}: async def {node.name}")
                elif isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                    offenders.append(f"{path.name}:{node.lineno}: "
                                     f"{type(node).__name__}")
        self.assertEqual(offenders, [],
                         "生产代码里还有异步结构（同步 WSGI 应用不该有）：\n"
                         + "\n".join(offenders))

    def test_production_code_has_no_uvicorn_or_asgi_references(self):
        offenders = []
        pattern = re.compile(r"uvicorn|\basgi\b", re.IGNORECASE)
        for path, source in _production_sources():
            for lineno, line in enumerate(source.splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "生产代码仍引用旧的异步 runtime：\n" + "\n".join(offenders))

    def test_requirements_do_not_pin_the_old_asgi_server(self):
        text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("uvicorn", text)
        self.assertIn("gunicorn", text)


if __name__ == "__main__":
    unittest.main()
