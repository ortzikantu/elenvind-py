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

from elenvind.core import config as config_module          # noqa: E402
from elenvind.core import db_base                          # noqa: E402
from elenvind.core import lifespan as lifespan_module      # noqa: E402
from elenvind.core import console as console_module        # noqa: E402
from elenvind.features.blog import logic as blog_logic     # noqa: E402
from elenvind.features.pages import logic as pages_logic   # noqa: E402

# 静音启动横幅/彩色日志：测试输出只保留 unittest 的结果
_NOOP = lambda *args, **kwargs: None
for _name in ("success", "error", "warning", "info", "banner"):
    setattr(lifespan_module, _name, _NOOP)
    setattr(console_module, _name, _NOOP)


def run_async(coro):
    """在同步测试里跑一个协程（每个测试独立事件循环，互不干扰）。"""
    return asyncio.run(coro)


def _remove_tmpdir(tmpdir, *, attempts=5):
    """删除用例临时目录；失败时**看得见**，并顺带清理同级的陈旧目录。

    为什么不用 `shutil.rmtree(..., ignore_errors=True)`：
    Windows 上若还有 sqlite 句柄没释放，rmtree 会抛
    `PermissionError: [WinError 32] ... being used by another process`，
    而 `ignore_errors=True` 把它彻底吞掉 —— 于是"清理失败"会安静地
    累积成几百个残留目录，谁也不知道清理逻辑什么时候坏掉的。
    改成：先重试几次（句柄释放有微小延迟），仍失败就打印到 stderr。

    另外顺手清掉 `.testtmp/` 下的**其它**目录：如果某次运行是被强杀的
    （中断、超时、崩溃），tearDown 根本没机会跑，那些目录会一直留着。
    每个用例开始时自愈一次，残留就不会无限增长。
    """
    import sys as _sys
    import time as _time

    for attempt in range(attempts):
        try:
            shutil.rmtree(tmpdir)
            break
        except FileNotFoundError:
            break
        except OSError as exc:
            if attempt == attempts - 1:
                print(f"[test-harness] could not remove {tmpdir}: {exc}",
                      file=_sys.stderr)
                break
            _time.sleep(0.05 * (attempt + 1))

    # 自愈：清掉父目录下**明显早已废弃**的陈旧目录（跳过当前这个）。
    #
    # 只删 mtime 超过 10 分钟的：如果有人用 `--parallel` 之类跑并发测试，
    # 兄弟目录可能正被另一个进程使用，凭"名字像 case-"就删会破坏它。
    # 正常一次 tearDown 到下一次测试开始是毫秒级，10 分钟只会命中
    # "上次运行被强杀（中断/超时/崩溃）留下的孤儿"。
    parent = Path(tmpdir).parent
    if not parent.name.startswith(".testtmp"):
        return
    import time as _time

    cutoff = _time.time() - 600
    try:
        for sibling in parent.iterdir():
            if sibling == Path(tmpdir) or not sibling.is_dir():
                continue
            if not sibling.name.startswith("case-"):
                continue
            try:
                if sibling.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(sibling, ignore_errors=True)
    except OSError:
        pass


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

    #: 默认直连对端：模拟"应用前面有一台受信代理"（127.0.0.1 在默认白名单内）
    DEFAULT_PEER = ("127.0.0.1", 44321)

    def __init__(self, host="example.com", scheme="https", client=None):
        self.host = host
        self.scheme = scheme
        self.client = client or self.DEFAULT_PEER
        self.started = False
        self.jar = {}          # use_jar=True 时的浏览器式 Cookie 罐

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
                body=None, send_content_length=True, extra_headers=(),
                use_jar=False):
        """构造一个"正常浏览器"风格的请求。

        `use_jar=True` 时使用浏览器式 Cookie 罐：响应 Set-Cookie 会被记住并
        自动带入后续请求。这更接近真实浏览器行为（也是 CSRF Double-Submit
        能正常工作的前提——令牌 Cookie 是随表单页一起下发的）。
        """
        if use_jar:
            merged = dict(self.jar)
            merged.update(cookies or {})
            cookies = merged
        header_list = [("host", self.host)]
        if cookies:
            header_list.append(("cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
        for key, value in (headers or {}).items():
            header_list.append((key, value))
        header_list.extend(extra_headers)

        raw_query = ""
        if query:
            raw_query = urlencode({k: v for k, v in query.items() if v is not None})

        # 与 Core 的 `http._FORM_METHODS` 对齐：这四种方法都会被解析表单，
        # 也都要求 Content-Length。曾经这里漏了 DELETE，于是
        # "DELETE 无 body" 在测试里表现为 400/405 而在真实请求里是 411，
        # 让方法级的测试写出与生产不符的期望。
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            if body is None:
                body = urlencode(form or {}).encode("utf-8")
            if content_type is not None:
                header_list.append(("content-type", content_type))
            if send_content_length:
                header_list.append(("content-length", str(len(body))))

        response = self.raw_request(method, path, raw_query.encode("latin-1"),
                                    header_list, body)
        if use_jar:
            self.jar.update(self.set_cookies(response))
        return response


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
        (self.tmpdir / "custom_pages").mkdir(parents=True)
        self.db_path = self.tmpdir / "test.db"

        # 每个测试独立的数据库文件：所有模块都通过 db_base.DB_PATH 取路径
        self._original_db_path = db_base.DB_PATH
        self._original_db_env = os.environ.get("ELENVIND_DB")
        db_base.DB_PATH = self.db_path
        os.environ["ELENVIND_DB"] = str(self.db_path)

        # 内容目录也隔离，避免读到真实 articles/custom_pages（setUp 已建好）
        self.articles_dir = self.tmpdir / "articles"
        self.custom_pages_dir = self.tmpdir / "custom_pages"

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
        # 清掉模块级缓存与启动钩子，避免测试之间串数据
        blog_logic._articles_cache = None
        blog_logic._file_stats = None
        blog_logic._body_cache.clear()
        blog_logic._failed_stats = {}
        pages_logic._pages_cache = None
        pages_logic._file_stats = None
        # 机会式清理的"上次清理时间"是模块级内存状态：不重置的话，
        # 前一个用例刚清理过会让后一个用例的清理被节流跳过（顺序耦合）。
        from elenvind.core.db_prune import reset_state as reset_prune_state
        reset_prune_state()
        lifespan_module.clear_startup_hooks()
        from elenvind.core.templating import reset_environment
        reset_environment()
        from elenvind.core.db_user import _invalidate_user_count
        _invalidate_user_count()
        _remove_tmpdir(self.tmpdir)

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
            "custom_pages_dir": str(self.custom_pages_dir),
            "server": {
                "host": "127.0.0.1",
                "port": 6789,
                "trusted_proxies": ["127.0.0.1", "::1"],
                "cookie_prefix": False,
            },
            "pagination": {"per_page": 6},
            "params": {"intro": "", "nav": [], "social": [], "projects": []},
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
    def write_article(self, slug, body, meta=None, *, touch=True):
        """写一篇测试文章：TOML front matter（+++ 包裹）+ Markdown 正文。

        默认推进 mtime：缓存失效基于 (mtime, size)，同一文件系统 tick 内
        连续写入会得到相同 mtime，让"内容变了"看起来像"没变"。
        这是文件系统精度问题而非产品行为，测试需要确定性。
        """
        fields = dict(meta or {"title": slug.title(), "date": "2026-01-01"})
        header_lines = ["+++"]
        for key, value in fields.items():
            if isinstance(value, str):
                header_lines.append(f'{key} = "{value}"')
            elif isinstance(value, bool):
                header_lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                header_lines.append(f"{key} = {value}")
            elif isinstance(value, list):
                items = ", ".join(f'"{v}"' for v in value)
                header_lines.append(f"{key} = [{items}]")
            else:
                raise TypeError(f"unsupported metadata value for {key}: {value!r}")
        header_lines.append("+++")
        path = self.articles_dir / f"{slug}.md"
        path.write_text("\n".join(header_lines) + "\n\n" + body + "\n", encoding="utf-8")
        return self.touch(path) if touch else path

    def write_page(self, slug, body, *, touch=True):
        """自定义页面：纯 Markdown 正文（无 front matter）。"""
        path = self.custom_pages_dir / f"{slug}.md"
        path.write_text(body, encoding="utf-8")
        return self.touch(path) if touch else path

    @staticmethod
    def touch(path, seconds=120):
        """显式推进文件 mtime（见 write_article 的说明）。

        偏移取 120 秒而不是几秒：小偏移在某些文件系统/时钟精度下会被
        近似成原值，导致缓存失效判定不触发。
        """
        stat = Path(path).stat()
        os.utime(path, (stat.st_atime + seconds, stat.st_mtime + seconds))
        return path

    def create_user(self, nickname="Alice", email="alice@example.com",
                    password="correct horse battery", is_admin=False):
        """直接建用户，返回 (user_id, password)。"""
        from elenvind.core.db_user import create_user
        from elenvind.core.security import hash_password

        user_id = create_user(nickname, email, hash_password(password))
        return user_id, password

    def login(self, email, password, csrf=None):
        """走真实的 POST /login 流程，返回响应（成功时响应里有 Set-Cookie）。"""
        token = csrf or self.fetch_csrf()
        return self.app.request("POST", "/login",
                                form={"email": email, "password": password,
                                      "csrf_token": token},
                                cookies=self.csrf_cookies(token))

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
        return token, self.csrf_cookies(token)

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
        """GET /login 拿一个 CSRF 令牌（含 Cookie 下发）。

        必须按**当前**的 Cookie 命名取（cookie_prefix 开启时是 `__Host-csrf`），
        否则开启前缀后所有依赖本方法的测试都会因令牌不匹配而失败。
        """
        from elenvind.core.security import CSRF_COOKIE, cookie_name

        response = self.app.request("GET", "/login")
        token = self.app.set_cookie_value(response, cookie_name(CSRF_COOKIE))
        if not token:
            token = self.app.set_cookie_value(response, CSRF_COOKIE)
        self.assertTrue(token, "login page did not issue a CSRF cookie")
        return token

    def csrf_cookies(self, token):
        """返回携带当前命名 CSRF Cookie 的 cookies 字典。"""
        from elenvind.core.security import CSRF_COOKIE, cookie_name
        return {cookie_name(CSRF_COOKIE): token}

    def csrf_cookie_name(self):
        from elenvind.core.security import CSRF_COOKIE, cookie_name
        return cookie_name(CSRF_COOKIE)

    def session_cookie_name(self):
        from elenvind.core.security import SESSION_COOKIE, cookie_name
        return cookie_name(SESSION_COOKIE)


def _cookie_value(set_cookie: str, name: str) -> str:
    """从 Set-Cookie 头里取出指定 Cookie 的值（测试辅助）。"""
    for part in set_cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""
