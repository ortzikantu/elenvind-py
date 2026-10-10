"""测试夹具：临时工作区（配置 / 数据库 / 日志 / 内容）+ WSGI 直调助手。

设计要点（为什么这样搭）：

- **不启动 Gunicorn**：直接调用 `elenvind.app.app(environ, start_response)`，
  走的是与生产完全相同的那条流水线（请求解析 → 会话 → 路由 → 安全头 → Cookie），
  但完全没有端口、进程与超时的不确定性。
- **不读仓库里的 config.toml**：`core.lifespan.SKIP_CONFIG_LOAD` 是测试专用开关
  （生产路径永远为 False），配置由本模块注入，`validate_config()` 照常执行 ——
  因此测试同时验证了"这套配置是合法的"。
- **一切可写的东西都落在 `tempfile.mkdtemp()` 里**：数据库、日志、文章、页面。
  运行测试不会碰仓库里的任何文件（`config.toml`、`sqlite.db`、`logs/` 都不动）。
- 应用与工作区在整个测试进程里**只启动一次**（`ensure_application()` 幂等）；
  用例之间用 `reset_database()` 清表隔离。
"""
from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
import sys
import tempfile
import urllib.parse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:                     # 允许 `python -m unittest`
    sys.path.insert(0, str(PROJECT_ROOT))

#: 测试用文章：两个（覆盖首页排序与文章页），内容里的 `<` 会被 markdown 层转义
ARTICLES = {
    "2026-01-01-first.md": (
        '+++\ntitle = "First Post"\ndate = 2026-01-01T00:00:00+00:00\n'
        'authors = ["Tester"]\n+++\n\nFirst body with `code` and **bold**.\n'
    ),
    "2026-01-02-second.md": (
        '+++\ntitle = "Second Post"\ndate = 2026-01-02T00:00:00+00:00\n+++\n\nSecond body.\n'
    ),
}
PAGES = {"about.md": "# About\n\nHello from a custom page.\n"}

ARTICLE_SLUG = "2026-01-01-first"
MISSING_SLUG = "no-such-slug"


class Workspace:
    """一个隔离的临时工作区。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="elenvind-tests-"))
        self.articles = self.root / "articles"
        self.pages = self.root / "pages"
        self.articles.mkdir()
        self.pages.mkdir()
        for name, text in ARTICLES.items():
            (self.articles / name).write_text(text, encoding="utf-8")
        for name, text in PAGES.items():
            (self.pages / name).write_text(text, encoding="utf-8")
        self.db = self.root / "sqlite.db"
        self.log = self.root / "app.log"


def test_config(workspace: Workspace) -> dict:
    """注入 `core.config.config` 的配置：与 config.example.toml 同构且更严格。

    刻意把限流阈值调小（锁定 3 次、评论配额 3 条、评论深度 3 层），
    这样"上限行为"可以在几次请求内被验证，而不必灌 1000 条数据。
    """
    return {
        "locale": "en",
        "title": "Elenvind Test",
        "copyright": "Tester",
        "site_url": "https://test.invalid",
        "admin_user_id": 1,
        "admin_badge": "ADMIN",
        "deleted_user_nickname": "Gone",
        "max_length": 1000,
        "max_comment_depth": 3,
        "max_comments_per_article": 50,
        "registration_enabled": True,
        "session_absolute_days": 30,
        "session_idle_days": 15,
        "max_body_size": 8192,
        "database": str(workspace.db),
        "articles_dir": str(workspace.articles),
        "custom_pages_dir": str(workspace.pages),
        "templates_dir": str(PROJECT_ROOT / "elenvind" / "templates"),
        "use_builtin_css": True,
        "server": {
            "host": "127.0.0.1",
            "port": 6789,
            "workers": 1,
            "trusted_proxies": ["127.0.0.1", "::1"],
            "cookie_prefix": False,
        },
        "pagination": {"per_page": 6},
        "comment_limits": {"max_per_user": 100, "max_per_ip": 100, "window_seconds": 60},
        "login_limits": {
            "max_email_failures": 3, "email_window_seconds": 86400,
            "max_ip_failures": 100, "ip_window_seconds": 900,
            "max_global_failures": 1000, "global_window_seconds": 900,
        },
        "register_limits": {"max_per_ip": 10000, "window_seconds": 1},
        "static": {"css": "", "favicon": "", "logo": "", "hero": ""},
        "logging": {
            "level": "info", "file": str(workspace.log),
            "max_bytes": 1048576, "backup_count": 1, "rotate": True,
        },
        "security": {
            "csp_enabled": True, "hsts_enabled": True, "hsts_max_age": 300,
            "hsts_include_subdomains": False, "permissions_policy": "",
            "csp": {},
        },
        "params": {
            "intro": "hello",
            "nav": [{"name": "About", "url": "/about"}],
            "social": [{"name": "Mail", "url": "mailto:me@test.invalid", "icon": ""}],
            "projects": [{"name": "Elenvind", "url": "https://example.test",
                          "description": "base"}],
        },
    }


WORKSPACE: Workspace | None = None
APP = None


def ensure_application():
    """幂等地准备临时工作区并完成一次 `startup()`，返回 WSGI callable。"""
    global WORKSPACE, APP
    if APP is not None:
        return APP
    WORKSPACE = Workspace()
    os.environ["ELENVIND_DB"] = str(WORKSPACE.db)

    from elenvind.core import config as config_module
    from elenvind.core import lifespan

    lifespan.SKIP_CONFIG_LOAD["value"] = True
    config_module.config.clear()
    config_module.config.update(test_config(WORKSPACE))

    from elenvind.app import STARTUP_HOOKS, app, prepare_database

    lifespan.startup(STARTUP_HOOKS, prepare_database=prepare_database)
    # 让测试输出保持干净：控制台 handler 只保留严重错误（**文件** handler 仍然是
    # 配置里的 info 级别，test_logging_proxy 断言的是文件内容）。
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.CRITICAL)
    APP = app
    return APP


def reset_database() -> None:
    """清空全部业务表与限流流水（用例之间隔离）。"""
    ensure_application()
    from elenvind.db import maintenance

    conn = sqlite3.connect(WORKSPACE.db)
    try:
        for table in ("login_attempts", "register_attempts", "comment_rate",
                      "comment", "session", "user"):
            conn.execute(f"DELETE FROM {table}")     # 表名是代码内字面量
        conn.commit()
    finally:
        conn.close()
    maintenance.reset_state()


def reload_content() -> None:
    """强制重扫文章 / 页面（绕过 2 秒扫描节流）。"""
    from elenvind.modules.blog import logic as blog_logic
    from elenvind.modules.pages import logic as pages_logic

    blog_logic.load_articles()
    pages_logic.load_pages()


def query(sql: str, args=()):
    """直接查临时库（仅测试使用；生产代码只能走 db 包）。"""
    ensure_application()
    conn = sqlite3.connect(WORKSPACE.db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def live_config() -> dict:
    """当前生效的配置字典（用例可临时改，务必在 finally 里恢复）。"""
    from elenvind.core.config import config

    ensure_application()
    return config


class Result:
    """一次直调的结果（状态码 / 响应头 / 响应体 / 新增 Cookie）。"""

    def __init__(self, status: str, headers, body: bytes) -> None:
        self.raw_status = status
        self.status = int(str(status).split(" ", 1)[0])
        self.headers = list(headers)
        self.body = body
        self.cookies: dict[str, str] = {}
        for name, value in self.headers:
            if name.lower() == "set-cookie":
                head = value.split(";", 1)[0]
                key, _, val = head.partition("=")
                self.cookies[key.strip()] = val.strip()

    def header(self, name, default=None):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return default

    def all_headers(self, name):
        return [value for key, value in self.headers if key.lower() == name.lower()]

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def request(method: str, path: str, *, form=None, body: bytes | None = None,
            headers=None, cookies=None, ip: str = "127.0.0.1",
            scheme: str = "http", content_length: bool = True,
            declared_length: int | None = None) -> Result:
    """向应用直调一个请求。

    - `content_length=False`：完全不声明长度（表单方法 → 411 契约）；
    - `declared_length=N`：声明一个与实际 body 不同的长度（截断 → 400 契约）。
    """
    app = ensure_application()
    if "?" in path:
        path, _, query_string = path.partition("?")
    else:
        query_string = ""

    payload = b""
    content_type = ""
    if form is not None:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    elif body is not None:
        payload = body
        content_type = "application/octet-stream"

    environ = {
        "REQUEST_METHOD": method.upper(),
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": ip,
        "HTTP_HOST": "testserver",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(payload),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
    }
    if content_length:
        environ["CONTENT_LENGTH"] = str(
            len(payload) if declared_length is None else declared_length)
    if content_type:
        environ["CONTENT_TYPE"] = content_type
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    if cookies:
        environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = response_headers

    chunks = app(environ, start_response)
    result = Result(captured.get("status", "500 Internal Server Error"),
                    captured.get("headers", []), b"".join(chunks))
    if not cookies:
        return result
    return result


class Session:
    """带 Cookie 罐的测试客户端（自动携带会话 / CSRF，自动合并 Set-Cookie）。"""

    def __init__(self, **options) -> None:
        self.cookies: dict[str, str] = {}
        self.options = options

    # ---------- 基础 ----------
    def get(self, path: str, **kwargs) -> Result:
        return self._call("GET", path, **kwargs)

    def post(self, path: str, form=None, **kwargs) -> Result:
        return self._call("POST", path, form=form, **kwargs)

    def _call(self, method: str, path: str, **kwargs) -> Result:
        options = dict(self.options)
        options.update(kwargs)
        cookies = dict(options.pop("cookies", self.cookies))
        result = request(method, path, cookies=cookies, **options)
        self.cookies.update(result.cookies)
        # 清除 Cookie（Max-Age=0）要从罐里删掉
        for name, value in list(result.cookies.items()):
            if value.strip('"') == "":      # Max-Age=0 的清除指令（SimpleCookie 会加引号）
                self.cookies.pop(name, None)
        return result

    # ---------- 令牌与账号流程 ----------
    def csrf(self, path: str) -> str | None:
        """从页面隐藏域取 CSRF 令牌（同时确保令牌 Cookie 已下发）。"""
        match = re.search(r'name="csrf_token" value="([^"]+)"', self.get(path).text())
        return match.group(1) if match else None

    def register(self, nickname: str, email: str, password: str) -> Result:
        return self.post("/register", {
            "nickname": nickname, "email": email, "password": password,
            "confirm_password": password, "csrf_token": self.csrf("/register"),
        })

    def login(self, email: str, password: str) -> Result:
        return self.post("/login", {
            "email": email, "password": password, "csrf_token": self.csrf("/login"),
        })

    def comment(self, slug: str, content: str, reply_to=None) -> Result:
        form = {"content": content, "csrf_token": self.csrf(f"/article/{slug}")}
        if reply_to is not None:
            form["reply_to"] = str(reply_to)
        return self.post(f"/article/{slug}/comment", form)

    def delete_comment(self, slug: str, comment_id: int) -> Result:
        return self.post(f"/article/{slug}/comment/delete/{comment_id}",
                         {"csrf_token": self.csrf(f"/article/{slug}")})

    def create_account(self, nickname="Tester", email="tester@example.test",
                       password="correct-horse-1") -> Result:
        result = self.register(nickname, email, password)
        if result.status == 302:
            self.login(email, password)
        return result
