"""冒烟驱动：用真实 Gunicorn（多 worker）+ 真实 config.toml 起一次服务并走完整流程。

不是测试套件的一部分（测试套件不需要 HTTP 服务器），只在需要"真实冷启动"
验证时手工运行：

    python smoke_driver.py        # 需要当前环境已安装 gunicorn

在仓库根目录运行。副作用隔离：数据库与它生成的临时内容落在
`.smoketmp/<随机名>/` 下，结束（含异常）时删除，不触碰仓库里的真实数据。

实现方式：Gunicorn 必须在**主线程**里接管信号，且它的配置是进程级的，
所以这里起一个真实的 gunicorn 子进程（`-w N`，默认 2 个 worker ——
多 worker 正是 C0 写协调要覆盖的场景），而不是在测试进程内跑服务器。
子进程通过一个生成的 shim 模块载入"临时配置 + 临时数据库"，
shim 路径经 PYTHONPATH 注入，因此不会碰仓库的 `config.toml`。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

from elenvind import db
from elenvind.db import connection as db_connection
from elenvind.core.config import ROOT, config, load_config, validate_config, apply_runtime_config

# 不使用 tempfile.mkdtemp：Windows 上它给出的目录权限仅创建者可写，
# 在受限沙箱里连自己的子目录都建不了。项目根下的 .smoketmp/ 继承正常权限。
TMP = Path(os.environ.get("ELENVIND_SMOKE_TMP", str(ROOT / ".smoketmp"))) / uuid.uuid4().hex[:10]
TMP.mkdir(parents=True)
PORT = int(os.environ.get("SMOKE_PORT", "6791"))
WORKERS = int(os.environ.get("SMOKE_WORKERS", "2"))
BASE = f"http://127.0.0.1:{PORT}"

#: 子进程入口 shim：把冒烟用的配置注入到应用里，再交给 Gunicorn。
#: 单独生成成文件（而不是 `python -c`）是因为内容较长，且要放进 PYTHONPATH。
SHIM_SOURCE = '''\
"""冒烟驱动生成的子进程入口（勿手工编辑）。"""
import json
import os

from elenvind.core import config as config_module
from elenvind import db
from elenvind.db import connection as db_connection
from elenvind.core import lifespan as lifespan_module

with open(os.environ["ELENVIND_SMOKE_CONFIG"], "r", encoding="utf-8") as handle:
    overrides = json.load(handle)

config_module.load_config()
config_module.config.update(overrides)
os.environ["ELENVIND_DB"] = overrides["database"]
config_module.validate_config()
config_module.apply_runtime_config()
# 让 startup() 沿用注入的配置（否则它会重读仓库 config.toml 覆盖临时路径）
lifespan_module.SKIP_CONFIG_LOAD["value"] = True

from elenvind.app import STARTUP_HOOKS, app as _app, prepare_database   # noqa: E402

# 数据库启动（路径解析 → 建表/迁移 → 清理）由装配层提供，与生产入口一致
lifespan_module.startup(STARTUP_HOOKS, prepare_database=prepare_database)
application = _app
'''


results = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(("PASS  " if condition else "FAIL  ") + label + (f"  [{detail}]" if detail and not condition else ""))


def build_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()),
                                       NoRedirect())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随 302，方便断言 Location 与 Set-Cookie。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(opener, method, path, data=None, headers=None):
    url = BASE + path
    body = None
    if data is not None:
        from urllib.parse import urlencode
        body = urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with opener.open(request, timeout=10) as response:
            return (response.status, response.read().decode("utf-8", "replace"),
                    {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as error:
        return (error.code, error.read().decode("utf-8", "replace"),
                {k.lower(): v for k, v in error.headers.items()})


def csrf_from(html):
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    return html[start:html.find('"', start)]


def prepare_config():
    """准备临时配置与内容，返回**要注入子进程 Gunicorn 的覆盖项**。

    驱动进程自己也套用同一份覆盖：这样才能用同一套值做前置断言
    （文章索引 / 自定义页面 / 会话窗口），并与服务器共享"同一份配置"的语义。
    """
    load_config()
    overrides = {
        "database": str(TMP / "smoke.db"),
        "articles_dir": str(TMP / "articles"),
        "custom_pages_dir": str(TMP / "custom_pages"),
        "logging": {"level": "critical", "file": str(TMP / "app.log")},
        "site_url": "https://smoke.example.com",
        # 固定为英文：避免依赖开发机上 config.toml 的 locale 设置，
        # 让下面基于文案的断言在任何环境下都稳定。
        "locale": "en",
        "static": {"css": "/static/style.css", "favicon": "/static/favicon.ico",
                   "logo": "/static/logo.png", "hero": "/static/hero.webp"},
        # 内置样式表默认开启（本 smoke 用外部 CSS；另有专门检查覆盖内置回落）
        "use_builtin_css": True,
    }
    config.update(overrides)

    (TMP / "articles").mkdir()
    (TMP / "custom_pages").mkdir()
    (TMP / "articles" / "smoke.md").write_text(
        '+++\ntitle = "Smoke Post"\ndate = "2026-01-01"\nauthors = ["Tester"]\n+++\n\n'
        "# Heading\n\nBody **bold** and `code` and [link](https://example.com).\n",
        encoding="utf-8")
    (TMP / "custom_pages" / "about.md").write_text("About **page**.", encoding="utf-8")
    os.environ["ELENVIND_DB"] = overrides["database"]
    db_connection.DB_PATH = Path(overrides["database"])
    validate_config()
    apply_runtime_config()
    return overrides


class SmokeServer:
    """一个真实的 Gunicorn 子进程（默认 2 worker），可带覆盖配置重启。

    为什么是子进程而不是本进程内起服务器：Gunicorn 要在**主线程**接管信号，
    且它的配置是进程级的全局状态；子进程同时也顺便验证了生产启动路径
    （`elenvind.wsgi` 风格的 shim + 多 worker）。
    """

    def __init__(self, overrides):
        self.base = dict(overrides)
        self.process = None
        self.log_path = TMP / "gunicorn.log"
        (TMP / "_smoke_entry.py").write_text(SHIM_SOURCE, encoding="utf-8")

    # ---------- 生命周期 ----------
    def start(self, extra=None):
        merged = dict(self.base)
        merged.update(extra or {})
        (TMP / "smoke_config.json").write_text(json.dumps(merged), encoding="utf-8")

        environment = dict(os.environ)
        environment["ELENVIND_SMOKE_CONFIG"] = str(TMP / "smoke_config.json")
        environment["ELENVIND_DB"] = merged["database"]
        paths = [str(ROOT), str(TMP)]
        if environment.get("PYTHONPATH"):
            paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(paths)

        command = [
            sys.executable, "-m", "gunicorn",
            "--bind", f"127.0.0.1:{PORT}",
            "--workers", str(WORKERS),
            "--log-level", "warning",
            # 与 config.toml [server].trusted_proxies 的出厂值一致
            "--forwarded-allow-ips", "127.0.0.1,::1",
            "_smoke_entry:application",
        ]
        self.log = open(self.log_path, "ab")
        self.process = subprocess.Popen(command, cwd=str(ROOT), env=environment,
                                        stdout=self.log, stderr=self.log)
        return self.process

    def wait_until_ready(self, timeout=25.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return False
            with socket.socket() as sock:
                sock.settimeout(0.25)
                if sock.connect_ex(("127.0.0.1", PORT)) == 0:
                    return True
            time.sleep(0.1)
        return False

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        try:
            self.log.close()
        except OSError:
            pass

    def restart(self, extra=None):
        """带覆盖配置重启（用于"改配置后行为应当变化"的检查）。"""
        self.stop()
        self.start(extra)
        if not self.wait_until_ready():
            raise RuntimeError(f"Gunicorn 重启失败；日志见 {self.log_path}")

    def log_tail(self, limit=2000):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""


def main():
    overrides = prepare_config()
    server = SmokeServer(overrides)
    server.start()
    check(f"Gunicorn 启动（{WORKERS} worker）", server.wait_until_ready(),
          server.log_tail())

    try:
        run_checks(server)
    finally:
        server.stop()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("-" * 60)
    print(f"{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    return 0


def run_checks(server):
    opener = build_opener()

    # 前置断言：测试文章/页面目录确实生效（否则后面所有页面断言都会误导）
    from elenvind.modules.blog import logic as blog_logic
    from elenvind.modules.pages import logic as pages_logic
    check("文章索引已装载", [a["slug"] for a in blog_logic.get_articles()] == ["smoke"],
          str([a["slug"] for a in blog_logic.get_articles()]))
    check("自定义页面已装载", pages_logic.get_page("about") is not None)

    status, body, headers = fetch(opener, "GET", "/")
    check("GET / 200", status == 200, status)
    check("首页列出文章", "Smoke Post" in body)
    check("安全响应头", headers.get("x-content-type-options") == "nosniff")
    check("CSP 禁用脚本", "script-src 'none'" in (headers.get("content-security-policy") or ""))
    check("页头站标结构", '<a class="header-brand" href="/">' in body)
    check("站标图标来自配置", "/logo.png" in body)

    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("文章页 200", status == 200, status)
    check("Markdown 渲染", "<strong>bold</strong>" in body and "<code>code</code>" in body)

    status, body, _ = fetch(opener, "GET", "/about")
    check("自定义页面", status == 200 and "<strong>page</strong>" in body)

    status, body, _ = fetch(opener, "GET", "/robots.txt")
    check("robots.txt", status == 200 and "https://smoke.example.com/sitemap.xml" in body)

    status, body, _ = fetch(opener, "GET", "/sitemap.xml")
    check("sitemap.xml", status == 200 and "/article/smoke" in body)

    # 样式表：配置有值就用配置的；缺省静态资源也必须可用
    check("配置的样式表被引用", 'href="/static/style.css"' in fetch(opener, "GET", "/")[1])
    status, css_body, css_headers = fetch(opener, "GET", "/css/style.css")
    check("缺省样式表可访问",
          status == 200 and "--primary" in css_body
          and (css_headers.get("content-type") or "").startswith("text/css"),
          f"{status} {css_headers.get('content-type')!r}")
    check("缺省样式表可缓存",
          "public" in (css_headers.get("cache-control") or "")
          and bool(css_headers.get("etag")),
          css_headers.get("cache-control"))

    # 缺省图标（elenvind/static/imgs/）必须能取到且是图像类型
    status, _icon_body, icon_headers = fetch(opener, "GET", "/imgs/favicon.png")
    check("缺省图标可访问",
          status == 200 and (icon_headers.get("content-type") or "").startswith("image/"),
          f"{status} {icon_headers.get('content-type')!r}")

    # 通用静态服务：往 static/ 里放文件即可按相对路径取到；越界一律 404
    probe = ROOT / "elenvind" / "static" / "imgs" / "smoke-probe.svg"
    probe.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    try:
        status, probe_body, probe_headers = fetch(
            opener, "GET", "/imgs/smoke-probe.svg")
        check("静态目录新增文件即可取到",
              status == 200 and "svg" in probe_body
              and (probe_headers.get("content-type") or "") == "image/svg+xml",
              f"{status} {probe_headers.get('content-type')!r}")
    finally:
        probe.unlink(missing_ok=True)

    traversal_blocked = True
    for hostile in ("/../config.toml", "/%2e%2e/config.toml",
                    "/imgs/../../config.toml", "/.gitignore",
                    "/imgs/.hidden"):
        code, hostile_body, _ = fetch(opener, "GET", hostile)
        if code != 404 or "site_url" in hostile_body:
            traversal_blocked = False
            break
    check("静态服务阻断路径穿越", traversal_blocked)

    # 把 [static] 清空后重启，确认真的回落到缺省资产（且链接是同源相对路径，
    # 因为 CSP 的 style-src 'self' 只允许同源样式）。
    #
    # 这里刻意**重启真实的 Gunicorn**（而不是改本进程的内存配置）：
    # 服务器在另一个进程里，改驱动进程的字典根本影响不到它 —— 旧实现正是
    # 因为服务器就在本进程内才能这么写；重启顺带又验证了一次多 worker 启动。
    server.restart({"static": {"css": "", "favicon": "", "logo": "",
                               "hero": "/static/hero.webp"}})
    try:
        _status, home, _ = fetch(opener, "GET", "/")
        check("配置为空时回落到缺省样式",
              'href="/css/style.css"' in home,
              [line for line in home.splitlines() if "stylesheet" in line][:2])
        check("配置为空时回落到缺省图标",
              'rel="icon" href="/imgs/favicon' in home,
              [line for line in home.splitlines() if 'rel="icon"' in line][:2])
        check("配置为空时站标用缺省图标",
              '<img src="/imgs/favicon' in home,
              [line for line in home.splitlines() if "header-brand" in line][:2])
    finally:
        server.restart()

    status, body, headers = fetch(opener, "GET", "/theme?mode=dark&next=/about")
    check("主题切换 302", status == 302 and headers.get("location") == "/about", status)

    status, _, _ = fetch(opener, "GET", "/definitely-missing")
    check("404", status == 404, status)

    # 方法不允许：仅声明 POST 的路径 GET 必须 405 且带 Allow。
    # 注意不要用 DELETE 探这个：需要请求体的方法缺 Content-Length 时先被 411 拦下，
    # 那样测到的是请求框架而不是路由方法判定。
    status, _, headers = fetch(opener, "GET", "/article/smoke/comment/delete/1")
    check("405 + Allow", status == 405 and "POST" in (headers.get("allow") or ""),
          f"{status} allow={headers.get('allow')!r}")

    status, _, _ = fetch(opener, "POST", "/definitely-missing", {})
    check("未知 POST 路径 405", status == 405, status)

    # 友好页：匿名访问 /user 与 GET /logout 都不该是 403/405
    status, body, _ = fetch(opener, "GET", "/user")
    check("匿名 /user 友好页", status == 200 and "/login?next=/user" in body, status)
    status, body, _ = fetch(opener, "GET", "/logout")
    check("GET /logout 确认页", status == 200 and "signed out" in body.lower(), status)

    # 匿名访问受保护页面 -> 跳登录并带回跳地址（开放重定向防护）
    status, _, headers = fetch(opener, "GET", "/admin")
    check("受保护页跳登录",
          status == 302 and (headers.get("location") or "").startswith("/login?next="),
          f"{status} {headers.get('location')!r}")
    status, body, _ = fetch(opener, "GET", "/login?next=%2F%2Fevil.example.com")
    check("next 不反射站外地址",
          status == 200 and 'value="//evil.example.com"' not in body, status)

    # 注册
    status, body, _ = fetch(opener, "GET", "/register")
    token = csrf_from(body)
    check("注册页含 CSRF", status == 200 and bool(token))
    status, body, _ = fetch(opener, "POST", "/register",
                            {"csrf_token": token, "nickname": "Smoke",
                             "email": "smoke@example.com", "password": "password-123",
                             "confirm_password": "password-123"})
    check("注册 302", status == 302, status)

    # 登录
    status, body, _ = fetch(opener, "GET", "/login")
    token = csrf_from(body)
    status, body, headers = fetch(opener, "POST", "/login",
                                  {"csrf_token": token, "email": "smoke@example.com",
                                   "password": "password-123"})
    check("登录 302", status == 302, status)
    check("会话 Cookie 属性",
          "HttpOnly" in (headers.get("set-cookie") or "")
          and "SameSite=Lax" in (headers.get("set-cookie") or ""))

    # 会话过期：两个时间戳都在，且绝对过期窗口决定 Cookie 寿命
    import sqlite3
    absolute_days = int(config.get("session_absolute_days", 30))
    idle_days = int(config.get("session_idle_days", 15))
    db_file = db_connection.DB_PATH
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(session)")}
        rows = conn.execute(
            "SELECT token, created_at, last_seen FROM session").fetchall()
    finally:
        conn.close()
    check("会话表有 created_at / last_seen",
          {"created_at", "last_seen"} <= columns and "expires" not in columns,
          sorted(columns))
    check("登录后写入一条会话", len(rows) == 1, len(rows))
    if rows:
        now = time.time()
        fresh = (now - rows[0]["created_at"] < 60
                 and now - rows[0]["last_seen"] < 60)
        check("新会话的时间戳是刚刚", fresh)
    check(f"绝对过期窗口 = {absolute_days} 天（Cookie Max-Age 对应）",
          f"Max-Age={absolute_days * 86400}" in (headers.get("set-cookie") or ""),
          headers.get("set-cookie"))
    check(f"滑动过期窗口 = {idle_days} 天（配置已生效）", idle_days > 0)

    # 个人中心
    status, body, _ = fetch(opener, "GET", "/user")
    check("个人中心", status == 200 and "smoke@example.com" in body)
    token = csrf_from(body)

    # 评论
    status, _, _ = fetch(opener, "POST", "/article/smoke/comment",
                         {"csrf_token": token, "content": "hello from smoke test"})
    check("发评论 302", status == 302, status)
    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("评论可见", "hello from smoke test" in body)

    # 回复评论：点"回复"会带 ?reply_to=N 重新打开文章页
    # （回归：这里曾因取单条评论时缺 user 列而 500）
    import sqlite3
    conn = sqlite3.connect(db_connection.DB_PATH)
    root_id = conn.execute("SELECT id FROM comment").fetchone()[0]
    conn.close()
    status, body, _ = fetch(opener, "GET", f"/article/smoke?reply_to={root_id}")
    check("回复页渲染 200", status == 200 and "Replying to" in body, status)
    check("回复表单带目标 id", f'value="{root_id}"' in body)
    status, _, _ = fetch(opener, "POST", "/article/smoke/comment",
                         {"csrf_token": token, "content": "nested reply",
                          "reply_to": str(root_id)})
    check("发回复 302", status == 302, status)
    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("回复可见且缩进", "nested reply" in body and "comment-reply" in body)
    # reply_to 指向不存在的评论：正常渲染，不报错
    status, body, _ = fetch(opener, "GET", "/article/smoke?reply_to=999999")
    check("未知 reply_to 不报错", status == 200, status)

    # 无 CSRF 的 POST 必须被拒
    status, _, _ = fetch(opener, "POST", "/logout", {"csrf_token": "x" * 43})
    check("坏 CSRF 400", status == 400, status)

    # 删评论
    import sqlite3
    conn = sqlite3.connect(db_connection.DB_PATH)
    comment_id = conn.execute("SELECT id FROM comment").fetchone()[0]
    conn.close()
    # 编辑评论（复用同一个输入框：编辑端点 + 预填的 GET 视图）
    status, body, _ = fetch(opener, "GET", f"/article/smoke?edit={comment_id}")
    import re as _re
    _ta = _re.search(r"<textarea[^>]*>(.*?)</textarea>", body, _re.S)
    check("编辑视图预填正文",
          'name="content"' in body and _ta is not None
          and _ta.group(1).strip() == "hello from smoke test",
          f"status={status} textarea={(_ta.group(1)[:40] if _ta else None)!r}")
    status, _, _ = fetch(opener, "POST", f"/article/smoke/comment/edit/{comment_id}",
                         {"csrf_token": token, "content": "edited by smoke"})
    check("编辑评论 302", status == 302, status)
    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("编辑后正文已更新",
          "edited by smoke" in body and "hello from smoke test" not in body, status)

    # 删评论 = 永久涂黑（原文从数据库里消失，且没有恢复入口）
    status, _, _ = fetch(opener, "POST", f"/article/smoke/comment/delete/{comment_id}",
                         {"csrf_token": token})
    check("删评论 302", status == 302, status)
    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("删除后涂黑", "is-redacted" in body and "edited by smoke" not in body, status)
    conn = sqlite3.connect(db_connection.DB_PATH)
    stored = conn.execute("SELECT content FROM comment WHERE id = ?",
                          (comment_id,)).fetchone()[0]
    conn.close()
    check("库里原文已被涂黑替换", set(stored) == {"\u2588"}, repr(stored[:8]))
    status, _, _ = fetch(opener, "POST", f"/article/smoke/comment/restore/{comment_id}",
                         {"csrf_token": token})
    check("恢复入口已移除", status in (404, 405), status)

    # 改密 → 强制重新登录
    status, _, headers = fetch(opener, "POST", "/user",
                               {"csrf_token": token, "action": "change_password",
                                "old_password": "password-123", "new_password": "new-password-456",
                                "confirm_password": "new-password-456"})
    check("改密 302 → /login", status == 302 and headers.get("location") == "/login", status)

    # 注销（新会话）：先看友好确认页，再真正 POST 退出
    status, body, _ = fetch(opener, "GET", "/login")
    token = csrf_from(body)
    fetch(opener, "POST", "/login", {"csrf_token": token, "email": "smoke@example.com",
                                     "password": "new-password-456"})
    status, body, _ = fetch(opener, "GET", "/logout")
    check("登录后 GET /logout 确认页",
          status == 200 and 'action="/logout"' in body and "csrf_token" in body, status)
    status, body, _ = fetch(opener, "GET", "/user")
    check("GET /logout 未销毁会话",
          status == 200 and "smoke@example.com" in body, status)
    status, _, headers = fetch(opener, "POST", "/logout", {"csrf_token": token})
    check("注销 302 → /", status == 302 and headers.get("location") == "/", status)
    status, body, _ = fetch(opener, "GET", "/user")
    check("注销后 /user 回到友好页",
          status == 200 and "smoke@example.com" not in body and "/login?next=/user" in body,
          status)

    # 大数据体：Content-Length 超限 → 413（直接发原始请求绕过 urllib 限制）
    import socket
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as sock:
        payload = b"a=1&" + b"b=" + b"x" * (2 * 1024 * 1024)
        request = (b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   b"Content-Type: application/x-www-form-urlencoded\r\n"
                   b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        sock.sendall(request)
        first_line = sock.recv(200).split(b"\r\n")[0]
    check("超大 body 413", b"413" in first_line, first_line)

    # 缺 Content-Length → 411
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as sock:
        sock.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Type: application/x-www-form-urlencoded\r\n\r\n")
        first_line = sock.recv(200).split(b"\r\n")[0]
    check("缺 Content-Length 411", b"411" in first_line, first_line)

    # 非表单 Content-Type → 415
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as sock:
        sock.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Type: application/json\r\n"
                     b"Content-Length: 2\r\n\r\n{}")
        first_line = sock.recv(200).split(b"\r\n")[0]
    check("非表单类型 415", b"415" in first_line, first_line)

    # Host 头投毒
    status, body, _ = fetch(opener, "GET", "/sitemap.xml",
                            headers={"Host": "evil.example.net"})
    check("Host 头无法投毒 sitemap", "evil.example.net" not in body)


if __name__ == "__main__":
    sys.exit(main())
