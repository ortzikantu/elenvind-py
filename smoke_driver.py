"""一次性冒烟驱动：用真实 uvicorn + 真实 config.toml 起一次服务并走完整流程。

不是测试套件的一部分（测试套件不依赖 uvicorn），只在需要"真实冷启动"验证时手工运行：

    python smoke_driver.py            # 需要当前环境已安装 uvicorn

数据库、日志、文章与自定义页面都重定向到系统临时目录，不触碰仓库里的真实数据。
"""
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

from elenvind import db_base
from elenvind.config import ROOT, config, load_config, validate_config, apply_runtime_config

# 不使用 tempfile.mkdtemp：Windows 上它给出的目录权限仅创建者可写，
# 在受限沙箱里连自己的子目录都建不了。项目根下的 .smoketmp/ 继承正常权限。
TMP = Path(os.environ.get("ELENVIND_SMOKE_TMP", str(ROOT / ".smoketmp"))) / uuid.uuid4().hex[:10]
TMP.mkdir(parents=True)
PORT = int(os.environ.get("SMOKE_PORT", "6791"))
BASE = f"http://127.0.0.1:{PORT}"

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
    from elenvind import lifespan as lifespan_module

    load_config()
    config["database"] = str(TMP / "smoke.db")
    config["articles_dir"] = str(TMP / "articles")
    config["usrpages_dir"] = str(TMP / "usrpages")
    config["logging"] = {"level": "critical", "file": str(TMP / "app.log")}
    config["site_url"] = "https://smoke.example.com"
    (TMP / "articles").mkdir()
    (TMP / "usrpages").mkdir()
    (TMP / "articles" / "smoke.evmd").write_text(
        '@@@\ntitle = "Smoke Post"\ndate = "2026-01-01"\nauthors = ["Tester"]\n@@@\n\n'
        "# Heading\n\nBody **bold** and `code` and @{link,https://example.com,link}.\n",
        encoding="utf-8")
    (TMP / "usrpages" / "about.evmd").write_text("About **page**.", encoding="utf-8")
    os.environ["ELENVIND_DB"] = str(TMP / "smoke.db")
    db_base.DB_PATH = TMP / "smoke.db"
    validate_config()
    apply_runtime_config()
    # 让 lifespan 沿用上面注入的配置（否则它会重新读仓库的 config.toml，覆盖测试路径）
    lifespan_module.SKIP_CONFIG_LOAD["value"] = True


def main():
    prepare_config()
    import uvicorn
    from elenvind.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                           log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    check("uvicorn 启动", server.started)

    try:
        run_checks()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("-" * 60)
    print(f"{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    return 0


def run_checks():
    opener = build_opener()

    # 前置断言：测试文章/页面目录确实生效（否则后面所有页面断言都会误导）
    from elenvind.articles import get_articles
    from elenvind.usrpages import get_page
    check("文章索引已装载", [a["slug"] for a in get_articles()] == ["smoke"],
          str([a["slug"] for a in get_articles()]))
    check("自定义页面已装载", get_page("about") is not None)

    status, body, headers = fetch(opener, "GET", "/")
    check("GET / 200", status == 200, status)
    check("首页列出文章", "Smoke Post" in body)
    check("安全响应头", headers.get("x-content-type-options") == "nosniff")
    check("CSP 禁用脚本", "script-src 'none'" in (headers.get("content-security-policy") or ""))

    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("文章页 200", status == 200, status)
    check("EVMD 渲染", "<strong>bold</strong>" in body and "<code>code</code>" in body)

    status, body, _ = fetch(opener, "GET", "/about")
    check("自定义页面", status == 200 and "<strong>page</strong>" in body)

    status, body, _ = fetch(opener, "GET", "/robots.txt")
    check("robots.txt", status == 200 and "https://smoke.example.com/sitemap.xml" in body)

    status, body, _ = fetch(opener, "GET", "/sitemap.xml")
    check("sitemap.xml", status == 200 and "/article/smoke" in body)

    status, body, headers = fetch(opener, "GET", "/theme?mode=dark&next=/about")
    check("主题切换 302", status == 302 and headers.get("location") == "/about", status)

    status, _, _ = fetch(opener, "GET", "/definitely-missing")
    check("404", status == 404, status)

    status, _, _ = fetch(opener, "DELETE", "/")
    check("405", status == 405, status)

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

    # 无 CSRF 的 POST 必须被拒
    status, _, _ = fetch(opener, "POST", "/logout", {"csrf_token": "x" * 43})
    check("坏 CSRF 400", status == 400, status)

    # 删评论
    import sqlite3
    conn = sqlite3.connect(db_base.DB_PATH)
    comment_id = conn.execute("SELECT id FROM comment").fetchone()[0]
    conn.close()
    status, _, _ = fetch(opener, "POST", f"/article/smoke/comment/delete/{comment_id}",
                         {"csrf_token": token})
    check("删评论 302", status == 302, status)
    status, body, _ = fetch(opener, "GET", "/article/smoke")
    check("删除后打码/划线", "is-deleted" in body)
    status, _, _ = fetch(opener, "POST", f"/article/smoke/comment/restore/{comment_id}",
                         {"csrf_token": token})
    check("恢复评论 302", status == 302, status)

    # 改密 → 强制重新登录
    status, _, headers = fetch(opener, "POST", "/user",
                               {"csrf_token": token, "action": "change_password",
                                "old_password": "password-123", "new_password": "new-password-456",
                                "confirm_password": "new-password-456"})
    check("改密 302 → /login", status == 302 and headers.get("location") == "/login", status)

    # 注销（新会话）
    status, body, _ = fetch(opener, "GET", "/login")
    token = csrf_from(body)
    fetch(opener, "POST", "/login", {"csrf_token": token, "email": "smoke@example.com",
                                     "password": "new-password-456"})
    status, _, headers = fetch(opener, "POST", "/logout", {"csrf_token": token})
    check("注销 302 → /", status == 302 and headers.get("location") == "/", status)

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
