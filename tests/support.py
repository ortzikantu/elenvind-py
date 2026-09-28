"""测试公共设施：ASGI 调用夹具、临时数据库、响应解析。

设计原则：
- 不依赖任何第三方测试包，只用标准库 unittest。
- 每个测试用例使用独立的临时数据库与临时内容目录，绝不触碰项目根的真实数据。
- 通过直接调用 ASGI 可调用对象（elenvind.app.app）来跑"端到端"请求，
  与 uvicorn 传入的 scope/receive/send 语义一致，因此无需安装 uvicorn 也能覆盖
  HTTP 层、路由、CSRF、会话与渲染的完整链路。
"""
import asyncio
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode

# 保证可以从项目根导入 elenvind 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 临时工作区：放测试数据库与临时内容目录。
# 默认落在项目根下的 .testtmp/（系统临时目录在受限沙箱里可能不可写），
# 每个测试自建子目录、tearDown 时删除。
TEST_TMP_ROOT = Path(os.environ.get("ELENVIND_TEST_TMP", str(PROJECT_ROOT / ".testtmp")))
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# 兜底：测试进程内任何一次 init_db() 都必须落到 .testtmp 下，
# 绝不能因为某个测试没走 ElenvindTestCase.setUp 就写到项目根的真实 sqlite.db。
os.environ.setdefault("ELENVIND_DB", str(TEST_TMP_ROOT / "fallback.db"))

# security.py 不再在导入期检查环境变量，但 validate_config() 会检查；
# 测试夹具统一注入一个测试用值。
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests")

from elenvind import config as config_module          # noqa: E402
from elenvind import db_base                          # noqa: E402
from elenvind import articles as articles_module      # noqa: E402
from elenvind import usrpages as usrpages_module      # noqa: E402
from elenvind import lifespan as lifespan_module      # noqa: E402
from elenvind import console as console_module        # noqa: E402

# 静音启动横幅/彩色日志：测试输出只保留 unittest 的结果
_NOOP = lambda *args, **kwargs: None
for _name in ("success", "error", "warning", "info", "banner"):
    setattr(lifespan_module, _name, _NOOP)
    setattr(console_module, _name, _NOOP)


def run_async(coro):
    """在同步测试里跑一个协程（每个测试独立事件循环，互不干扰）。"""
    return asyncio.run(coro)


class Response:
    """ASGI 响应的解析结果。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers                  # list[(bytes, bytes)]
        self.body = body                        # bytes

    def header(self, name: str):
        """按名字取响应头（同名返回最后一个），不存在返回 None。"""
        wanted = name.lower().encode("latin-1")
        found = None
        for key, value in self.headers:
            if key.lower() == wanted:
                found = value.decode("latin-1")
        return found

    def headers_all(self, name: str):
        wanted = name.lower().encode("latin-1")
        return [value.decode("latin-1") for key, value in self.headers if key.lower() == wanted]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return self.header("content-type") or ""


class AppHarness:
    """把 elenvind.app.app 当作真实 ASGI 应用来调用。"""

    def __init__(self, host="example.com", scheme="https", client=("203.0.113.7", 44321)):
        self.host = host
        self.scheme = scheme
        self.client = client
        self.started = False

    # ---------- lifespan ----------
    def startup(self):
        """跑一次完整的 lifespan 启动流程（配置/日志/i18n/建库/缓存）。

        启动失败时把失败原因作为断言抛出，避免测试报出与真实原因无关的错误。
        """
        from elenvind.app import app

        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        sent = []

        async def receive():
            if messages:
                return messages.pop(0)
            return {"type": "lifespan.shutdown"}

        async def send(message):
            sent.append(message)

        run_async(app({"type": "lifespan"}, receive, send))
        self.started = True
        first = sent[0] if sent else {}
        if first.get("type") != "lifespan.startup.complete":
            raise AssertionError(f"lifespan startup failed: {first}")
        return first

    # ---------- HTTP ----------
    # ---------- 便捷操作 ----------
    def set_cookie_value(self, response, name: str) -> str:
        """从响应的多个 Set-Cookie 头里取出指定 Cookie 的值。"""
        return self.set_cookies(response).get(name, "")

    def set_cookies(self, response) -> dict:
        """把响应里所有 Set-Cookie 解析成 {name: value}（含清除用的空值）。"""
        jar = {}
        for header in response.headers_all("set-cookie"):
            pair = header.split(";", 1)[0]
            key, _, value = pair.strip().partition("=")
            jar[key] = value.strip().strip('"')
        return jar

    def raw_request(self, method, path, query_string=b"", headers=(),
                    body=b"", chunks=None, client=None):
        """最底层调用：headers/body/chunks 完全由调用方给定。

        chunks 给出时按分片发送（用于测试分片 body、提前结束、断开等场景）。
        """
        from elenvind.app import app

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": self.scheme,
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": query_string,
            "root_path": "",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1"))
                        for k, v in headers],
            "client": client if client is not None else self.client,
            "server": (self.host, 443),
        }

        if chunks is None:
            messages = [{"type": "http.request", "body": body, "more_body": False}]
        else:
            messages = list(chunks)

        sent = []
        received_body = [False]

        async def receive():
            if messages:
                return messages.pop(0)
            # 客户端不再发数据：模拟断开，避免被测代码无限等待
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        run_async(app(scope, receive, send))

        status = 500
        resp_headers = []
        resp_body = b""
        for message in sent:
            if message["type"] == "http.response.start":
                status = message["status"]
                resp_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                resp_body += message.get("body", b"")
        return Response(status, resp_headers, resp_body)

    def request(self, method, path, *, query=None, form=None, cookies=None,
                headers=None, content_type="application/x-www-form-urlencoded",
                body=None, send_content_length=True, extra_headers=()):
        """构造一个"正常浏览器"风格的请求。"""
        header_list = [("host", self.host)]
        if cookies:
            header_list.append(("cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
        for key, value in (headers or {}).items():
            header_list.append((key, value))
        header_list.extend(extra_headers)

        raw_query = ""
        if query:
            raw_query = urlencode({k: v for k, v in query.items() if v is not None})

        if method in ("POST", "PUT", "PATCH"):
            if body is None:
                body = urlencode(form or {}).encode("utf-8")
            if content_type is not None:
                header_list.append(("content-type", content_type))
            if send_content_length:
                header_list.append(("content-length", str(len(body))))

        return self.raw_request(method, path, raw_query.encode("latin-1"),
                                header_list, body)


class ElenvindTestCase(unittest.TestCase):
    """所有测试的基类：提供临时数据库 + 临时内容目录 + 应用夹具。"""

    #: 子类可覆盖：是否需要真实加载 config.toml
    use_real_config = False

    def setUp(self):
        # 注意：不用 tempfile.mkdtemp —— 它在 Windows 上会给出仅创建者可写的 ACL，
        # 在受限沙箱里连自己的子目录都建不了。手工建目录继承父目录权限即可。
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmpdir = TEST_TMP_ROOT / f"case-{uuid.uuid4().hex[:12]}"
        (self.tmpdir / "articles").mkdir(parents=True)
        (self.tmpdir / "usrpages").mkdir(parents=True)
        self.db_path = self.tmpdir / "test.db"

        # 每个测试独立的数据库文件：所有模块都通过 db_base.DB_PATH 取路径
        self._original_db_path = db_base.DB_PATH
        self._original_db_env = os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = self.db_path
        os.environ["ELENVIND_DB"] = str(self.db_path)

        # 内容目录也隔离，避免读到真实 articles/usrpages（setUp 已建好）
        self.articles_dir = self.tmpdir / "articles"
        self.usrpages_dir = self.tmpdir / "usrpages"

        self._original_config = dict(config_module.config)
        config_module.config.clear()
        config_module.config.update(self.base_config())
        #: 当前测试生效的配置字典（与 elenvind.config.config 是同一个对象）
        self._config = config_module.config

        # 夹具自带配置：启动阶段不要读磁盘上的 config.toml
        lifespan_module.SKIP_CONFIG_LOAD["value"] = True

        self.app = AppHarness()
        self._prepare_runtime()
        self.app.startup()

    def _prepare_runtime(self):
        """测试环境的额外准备工作（子类可覆盖，如加载真实 config.toml）。"""

    def tearDown(self):
        lifespan_module.SKIP_CONFIG_LOAD["value"] = False
        db_base.DB_PATH = self._original_db_path
        if self._original_db_env is None:
            os.environ.pop("ELENVIND_DB", None)
        else:
            os.environ["ELENVIND_DB"] = self._original_db_env
        config_module.config.clear()
        config_module.config.update(self._original_config)
        # 清掉模块级缓存，避免测试之间串数据
        articles_module._articles_cache = None
        articles_module._file_stats = None
        articles_module._body_cache.clear()
        articles_module._failed_stats = {}
        usrpages_module._pages_cache = None
        usrpages_module._file_stats = None
        from elenvind.db_user import _invalidate_user_count
        _invalidate_user_count()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def base_config(self) -> dict:
        """测试用的最小可用配置（等价于 config.toml 的结构）。"""
        return {
            "locale": "en",
            "title": "Test Site",
            "copyright": "Test",
            "description": "Test site",
            "site_url": "https://example.com",
            "admin_user_id": 1,
            "admin_badge": "ADMIN",
            "deleted_user_nickname": "Journeyed On",
            "max_length": 1000,
            "max_comment_depth": 8,
            "max_comments_per_article": 500,
            "registration_enabled": True,
            "max_body_size": 1024 * 1024,
            "database": "sqlite.db",
            "articles_dir": str(self.articles_dir),
            "usrpages_dir": str(self.usrpages_dir),
            "server": {
                "host": "127.0.0.1",
                "port": 6789,
                "trusted_proxies": ["127.0.0.1", "::1"],
                "cookie_prefix": False,
            },
            "pagination": {"per_page": 6},
            "comment_limits": {"max_per_user": 5, "max_per_ip": 10, "window_seconds": 60},
            "login_limits": {
                "max_email_failures": 5, "email_window_seconds": 86400,
                "max_ip_failures": 20, "ip_window_seconds": 900,
                "max_global_failures": 200, "global_window_seconds": 900,
            },
            "register_limits": {"max_per_ip": 5, "window_seconds": 3600},
            "logging": {"level": "critical", "file": str(self.tmpdir / "app.log")},
        }

    # ---------- 便捷操作 ----------
    def write_article(self, slug, body, meta=None):
        """写一篇带文档头的测试文章。"""
        header_lines = ["@@@"]
        for key, value in (meta or {"title": slug.title(), "date": "2026-01-01"}).items():
            if isinstance(value, str):
                header_lines.append(f'{key} = "{value}"')
            elif isinstance(value, list):
                items = ", ".join(f'"{v}"' for v in value)
                header_lines.append(f"{key} = [{items}]")
            else:
                header_lines.append(f"{key} = {value}")
        header_lines.append("@@@")
        path = self.articles_dir / f"{slug}.evmd"
        path.write_text("\n".join(header_lines) + "\n\n" + body + "\n", encoding="utf-8")
        return path

    def write_page(self, slug, body):
        path = self.usrpages_dir / f"{slug}.evmd"
        path.write_text(body, encoding="utf-8")
        return path

    def create_user(self, nickname="Alice", email="alice@example.com",
                    password="correct horse battery", is_admin=False):
        """直接建用户，返回 (user_id, password)。"""
        from elenvind.db_user import create_user
        from elenvind.security import hash_password

        user_id = create_user(nickname, email, hash_password(password))
        return user_id, password

    def login(self, email, password, csrf=None):
        """走真实的 POST /login 流程，返回响应（成功时响应里有 Set-Cookie）。"""
        token = csrf or self.fetch_csrf()
        return self.app.request("POST", "/login",
                                form={"email": email, "password": password,
                                      "csrf_token": token},
                                cookies={self.csrf_cookie_name(): token})

    def login_ok(self, email, password):
        """登录并返回 (session_token, csrf_token)；失败时断言失败。"""
        response = self.login(email, password)
        self.assertEqual(response.status, 302, response.text[:400])
        session = self.app.set_cookie_value(response, self.session_cookie_name())
        self.assertTrue(session, "login did not issue a session cookie")
        return session, self.fetch_csrf()

    def new_session(self):
        """为一个全新客户端取出 (csrf_token, cookies 字典)。"""
        token = self.fetch_csrf()
        return token, {self.csrf_cookie_name(): token}

    def app_cookies(self, session=None, csrf=None, theme=None):
        jar = {}
        if session:
            jar[self.session_cookie_name()] = session
        if csrf:
            jar[self.csrf_cookie_name()] = csrf
        if theme:
            jar["theme"] = theme
        return jar

    def fetch_csrf(self):
        """GET /login 拿一个 CSRF 令牌（含 Cookie 下发）。"""
        response = self.app.request("GET", "/login")
        return self.app.set_cookie_value(response, "csrf")

    def csrf_cookie_name(self):
        from elenvind.security import CSRF_COOKIE
        return CSRF_COOKIE

    def session_cookie_name(self):
        from elenvind.security import SESSION_COOKIE
        return SESSION_COOKIE


def _cookie_value(set_cookie: str, name: str) -> str:
    """从 Set-Cookie 头里取出指定 Cookie 的值（测试辅助）。"""
    for part in set_cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""
