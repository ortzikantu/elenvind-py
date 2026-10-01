"""配置闭环测试：每一个配置项都必须"改了就真的生效"。

这是本项目最关键的测试类之一：它把"配置文件看起来支持但代码里硬编码"
这类问题钉死在回归测试里。每个断言都是"改配置 → 观察行为变化"。

另外包含：
- 死配置守卫（config.example.toml 里每个键都必须被 runtime 读取）；
- database 路径优先级（ELENVIND_DB > config.database > 默认 sqlite.db）。
"""
import ast
import os
import re
import tomllib
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT, ElenvindTestCase

from elenvind.core import db_base
from elenvind.core.config import ROOT
from elenvind.core.db_comment import get_comments_by_article


class ConfigBehaviorTests(ElenvindTestCase):
    """改配置 → 行为改变。"""

    # ---------- 评论相关 ----------
    def test_max_length_is_enforced(self):
        self.write_article("post", "body")
        self.create_user(email="len@example.com")
        session, csrf = self.login_ok("len@example.com", "correct horse battery")
        cookies = self.app_cookies(session=session, csrf=csrf)

        self._config["max_length"] = 5
        response = self.app.request("POST", "/article/post/comment",
                                    form={"csrf_token": csrf, "content": "123456"},
                                    cookies=cookies)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(get_comments_by_article("post")), 0)

        self._config["max_length"] = 100
        response = self.app.request("POST", "/article/post/comment",
                                    form={"csrf_token": csrf, "content": "123456"},
                                    cookies=cookies)
        self.assertEqual(response.status, 302)
        self.assertEqual(len(get_comments_by_article("post")), 1)

    def test_max_comment_depth_is_enforced(self):
        self.write_article("post", "body")
        self.create_user(email="depth@example.com")
        session, csrf = self.login_ok("depth@example.com", "correct horse battery")
        cookies = self.app_cookies(session=session, csrf=csrf)

        self._config["max_comment_depth"] = 2
        first = self.app.request("POST", "/article/post/comment",
                                 form={"csrf_token": csrf, "content": "L1"}, cookies=cookies)
        self.assertEqual(first.status, 302)
        parent = get_comments_by_article("post")[-1]["id"]

        second = self.app.request("POST", "/article/post/comment",
                                  form={"csrf_token": csrf, "content": "L2",
                                        "reply_to": str(parent)}, cookies=cookies)
        self.assertEqual(second.status, 302)
        child = get_comments_by_article("post")[-1]["id"]

        third = self.app.request("POST", "/article/post/comment",
                                 form={"csrf_token": csrf, "content": "L3",
                                       "reply_to": str(child)}, cookies=cookies)
        self.assertEqual(third.status, 400)   # 第 3 层被拒
        self.assertEqual(len(get_comments_by_article("post")), 2)

        # 放宽后同一层级即可通过 —— 证明是配置在起作用，而不是硬编码
        self._config["max_comment_depth"] = 5
        third = self.app.request("POST", "/article/post/comment",
                                 form={"csrf_token": csrf, "content": "L3",
                                       "reply_to": str(child)}, cookies=cookies)
        self.assertEqual(third.status, 302)
        self.assertEqual(len(get_comments_by_article("post")), 3)

    def test_max_comments_per_article_is_enforced(self):
        self.write_article("post", "body")
        self.create_user(email="cap@example.com")
        session, csrf = self.login_ok("cap@example.com", "correct horse battery")
        cookies = self.app_cookies(session=session, csrf=csrf)
        self._config["comment_limits"] = {"max_per_user": 100, "max_per_ip": 100,
                                          "window_seconds": 60}
        self._config["max_comments_per_article"] = 2

        for index in range(2):
            response = self.app.request("POST", "/article/post/comment",
                                        form={"csrf_token": csrf,
                                              "content": f"c{index}"}, cookies=cookies)
            self.assertEqual(response.status, 302)
        blocked = self.app.request("POST", "/article/post/comment",
                                   form={"csrf_token": csrf, "content": "c3"}, cookies=cookies)
        self.assertEqual(blocked.status, 429)
        self.assertEqual(len(get_comments_by_article("post")), 2)

    def test_comment_limits_are_read_from_config(self):
        self.write_article("post", "body")
        self.create_user(email="cl@example.com")
        session, csrf = self.login_ok("cl@example.com", "correct horse battery")
        cookies = self.app_cookies(session=session, csrf=csrf)

        # 默认（测试基类配置）允许 5 条
        self._config["comment_limits"] = {"max_per_user": 2, "max_per_ip": 100,
                                          "window_seconds": 60}
        for index in range(2):
            self.app.request("POST", "/article/post/comment",
                             form={"csrf_token": csrf, "content": f"c{index}"},
                             cookies=cookies)
        blocked = self.app.request("POST", "/article/post/comment",
                                   form={"csrf_token": csrf, "content": "third"},
                                   cookies=cookies)
        self.assertEqual(blocked.status, 429)

        # 提高阈值后即可继续（同一账号、同一 IP、同一窗口）
        self._config["comment_limits"] = {"max_per_user": 50, "max_per_ip": 100,
                                          "window_seconds": 60}
        allowed = self.app.request("POST", "/article/post/comment",
                                   form={"csrf_token": csrf, "content": "third"},
                                   cookies=cookies)
        self.assertEqual(allowed.status, 302)

    # ---------- HTTP 请求体 ----------
    def test_max_body_size_is_enforced(self):
        for limit, body_len, expected in ((100, 80, 400),      # 未超限：进业务逻辑
                                          (100, 200, 413),     # 声明超限
                                          (4096, 3000, 400)):  # 未超限
            with self.subTest(limit=limit, body_len=body_len):
                self._config["max_body_size"] = limit
                body = b"a=" + b"x" * (body_len - 2)
                response = self.app.raw_request(
                    "POST", "/login", b"",
                    [("host", "example.com"),
                     ("content-type", "application/x-www-form-urlencoded"),
                     ("content-length", str(len(body)))],
                    body=body)
                self.assertEqual(response.status, expected)

    # ---------- SEO ----------
    def test_site_url_drives_sitemap_and_robots(self):
        self.write_article("seo-post", "body", {"title": "SEO", "date": "2026-01-01"})
        self._config["site_url"] = "https://config-behaviour.test"
        sitemap = self.app.request("GET", "/sitemap.xml").text
        self.assertIn("https://config-behaviour.test/", sitemap)
        self.assertIn("https://config-behaviour.test/article/seo-post", sitemap)
        robots = self.app.request("GET", "/robots.txt").text
        self.assertIn("Sitemap: https://config-behaviour.test/sitemap.xml", robots)

        self._config["site_url"] = "https://other.test"
        sitemap = self.app.request("GET", "/sitemap.xml").text
        self.assertIn("https://other.test/article/seo-post", sitemap)
        self.assertNotIn("config-behaviour.test", sitemap)

        self._config["site_url"] = ""
        self.assertNotIn("<loc>", self.app.request("GET", "/sitemap.xml").text)
        self.assertNotIn("Sitemap:", self.app.request("GET", "/robots.txt").text)

    # ---------- 注册 ----------
    def test_registration_enabled_switch(self):
        self._config["registration_enabled"] = False
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/register",
                                    form={"csrf_token": csrf, "nickname": "X",
                                          "email": "off@example.com",
                                          "password": "password-123",
                                          "confirm_password": "password-123"},
                                    cookies={"csrf": csrf})
        self.assertIn("closed", response.text.lower())
        from elenvind.core.db_user import get_user_by_email
        self.assertIsNone(get_user_by_email("off@example.com"))

        self._config["registration_enabled"] = True
        csrf = self.fetch_csrf()
        response = self.app.request("POST", "/register",
                                    form={"csrf_token": csrf, "nickname": "X",
                                          "email": "off@example.com",
                                          "password": "password-123",
                                          "confirm_password": "password-123"},
                                    cookies={"csrf": csrf})
        self.assertEqual(response.status, 302)
        self.assertIsNotNone(get_user_by_email("off@example.com"))

    def test_register_limits_are_read_from_config(self):
        self._config["register_limits"] = {"max_per_ip": 1, "window_seconds": 3600}
        csrf = self.fetch_csrf()
        cookies = {"csrf": csrf}
        first = self.app.request("POST", "/register",
                                 form={"csrf_token": csrf, "nickname": "A",
                                       "email": "a@example.com", "password": "password-123",
                                       "confirm_password": "password-123"}, cookies=cookies)
        self.assertEqual(first.status, 302)
        second = self.app.request("POST", "/register",
                                  form={"csrf_token": csrf, "nickname": "B",
                                        "email": "b@example.com", "password": "password-123",
                                        "confirm_password": "password-123"}, cookies=cookies)
        self.assertIn("Too many registration attempts", second.text)

        self._config["register_limits"] = {"max_per_ip": 10, "window_seconds": 3600}
        third = self.app.request("POST", "/register",
                                 form={"csrf_token": csrf, "nickname": "B",
                                       "email": "b@example.com", "password": "password-123",
                                       "confirm_password": "password-123"}, cookies=cookies)
        self.assertEqual(third.status, 302)

    # ---------- 登录限流 ----------
    def test_login_limits_are_read_from_config(self):
        self.create_user(email="lock@example.com")
        self._config["login_limits"] = {
            "max_email_failures": 2, "email_window_seconds": 86400,
            "max_ip_failures": 1000, "ip_window_seconds": 900,
            "max_global_failures": 1000, "global_window_seconds": 900,
        }
        self.login("lock@example.com", "wrong")
        self.login("lock@example.com", "wrong")
        locked = self.login("lock@example.com", "correct horse battery")
        self.assertIn("Too many failed attempts for this account", locked.text)

        # 提高阈值 → 同一账号立刻可以再尝试
        self._config["login_limits"]["max_email_failures"] = 50
        allowed = self.login("lock@example.com", "correct horse battery")
        self.assertEqual(allowed.status, 302)

    def test_global_login_limit_is_read_from_config(self):
        self._config["login_limits"] = {
            "max_email_failures": 100, "email_window_seconds": 86400,
            "max_ip_failures": 100, "ip_window_seconds": 900,
            "max_global_failures": 1, "global_window_seconds": 900,
        }
        self.login("g1@example.com", "wrong")
        blocked = self.login("g2@example.com", "wrong")
        self.assertIn("Too many failed attempts. Please try again later.", blocked.text)

    # ---------- 管理员 ----------
    def test_admin_user_id_controls_privileges(self):
        other_id, _ = self.create_user(nickname="Other", email="other@example.com")
        self.write_article("post", "body")
        from elenvind.core.db_comment import create_comment
        comment_id = create_comment("post", other_id, "content")

        # 默认 admin_user_id = 1：id=1 的账号不是管理员时不能恢复
        self._config["admin_user_id"] = other_id
        self.create_user(nickname="First", email="first@example.com")
        session, csrf = self.login_ok("other@example.com", "correct horse battery")
        cookies = self.app_cookies(session=session, csrf=csrf)
        from elenvind.core.db_comment import soft_delete_comment, get_comment_by_id
        soft_delete_comment(comment_id)
        response = self.app.request("POST", f"/article/post/comment/restore/{comment_id}",
                                    form={"csrf_token": csrf}, cookies=cookies)
        self.assertEqual(response.status, 302)
        self.assertEqual(get_comment_by_id(comment_id)["is_deleted"], 0)

        # 指向别的 id 后同一个人失去权限
        self._config["admin_user_id"] = 999
        soft_delete_comment(comment_id)
        denied = self.app.request("POST", f"/article/post/comment/restore/{comment_id}",
                                  form={"csrf_token": csrf}, cookies=cookies)
        self.assertEqual(denied.status, 403)

    def test_admin_badge_is_configurable(self):
        self._config["admin_badge"] = "SUPERVISOR"
        self.create_user(nickname="Boss", email="boss@example.com")
        session, csrf = self.login_ok("boss@example.com", "correct horse battery")
        page = self.app.request("GET", "/", cookies=self.app_cookies(session=session,
                                                                     csrf=csrf)).text
        self.assertIn("SUPERVISOR", page)

        self._config["admin_badge"] = ""
        page = self.app.request("GET", "/", cookies=self.app_cookies(session=session,
                                                                     csrf=csrf)).text
        self.assertNotIn("SUPERVISOR", page)

    # ---------- 展示类配置 ----------
    def test_title_and_copyright_are_used(self):
        self._config["title"] = "Behaviour Site"
        self._config["copyright"] = "Behaviour Owner"
        page = self.app.request("GET", "/").text
        self.assertIn("<title>Behaviour Site</title>", page)
        self.assertIn("Behaviour Owner", page)

        self._config["title"] = "Renamed Site"
        self.assertIn("<title>Renamed Site</title>", self.app.request("GET", "/").text)

    def test_locale_changes_rendered_language(self):
        self._config["locale"] = "en"
        english = self.app.request("GET", "/login").text
        self.assertIn('<html lang="en">', english)

        self._config["locale"] = "ja"
        japanese = self.app.request("GET", "/login").text
        self.assertIn('<html lang="ja">', japanese)
        self.assertNotEqual(english, japanese)

        self._config["locale"] = "zh-CN"
        self.assertIn('<html lang="zh">', self.app.request("GET", "/login").text)

    def test_deleted_user_nickname_is_used(self):
        user_id, _ = self.create_user(nickname="Leaver", email="leaver@example.com")
        self.write_article("post", "body")
        from elenvind.core.db_comment import create_comment
        create_comment("post", user_id, "old comment")
        from elenvind.core.db_user import delete_user
        delete_user(user_id)

        self._config["deleted_user_nickname"] = "GONE-AWAY"
        page = self.app.request("GET", "/article/post").text
        self.assertIn("GONE-AWAY", page)

    def test_pagination_per_page_is_used(self):
        for index in range(4):
            self.write_article(f"p{index}", "body",
                               {"title": f"Post {index}", "date": f"2026-01-0{index + 1}"})
        self._config["pagination"] = {"per_page": 2}
        page = self.app.request("GET", "/").text
        self.assertIn("Page 1 of 2", page)

        self._config["pagination"] = {"per_page": 4}
        page = self.app.request("GET", "/").text
        self.assertEqual(page.count("/article/p"), 4)

    def test_static_urls_are_used_in_html(self):
        self._config["static"] = {"css": "/assets/site.css",
                                  "favicon": "/assets/icon.ico",
                                  "logo": "/assets/logo.png",
                                  "hero": "/assets/hero.webp"}
        page = self.app.request("GET", "/").text
        self.assertIn('href="/assets/site.css"', page)
        self.assertIn('href="/assets/icon.ico"', page)
        self.assertIn("/assets/hero.webp", page)
        self.assertIn('<a class="header-brand" href="/">', page)
        self.assertIn('src="/assets/logo.png"', page)

    def test_logo_falls_back_to_favicon_then_to_text_only(self):
        self._config["static"] = {"favicon": "/assets/icon.ico"}
        page = self.app.request("GET", "/").text
        self.assertIn('<a class="header-brand" href="/">', page)
        self.assertIn('src="/assets/icon.ico"', page)

        self._config["static"] = {}
        page = self.app.request("GET", "/").text
        self.assertIn('<a class="header-brand" href="/">', page)
        self.assertNotIn("header-brand\"><img", page)
        self.assertIn(f"<span class=\"header-title\">{self._config['title']}</span>", page)

    def test_templates_dir_change_switches_template_source(self):
        """templates_dir 必须真的生效：换目录就换模板（不是死配置）。

        注意它是一个**完整替换**：目录必须自带被请求的模板，
        所以这里同时提供 base.html 与 home.html。
        """
        from elenvind.core.templating import reset_environment

        custom = self.tmpdir / "custom-templates"
        (custom / "partials").mkdir(parents=True)
        (custom / "base.html").write_text(
            "<!DOCTYPE html><html lang=\"{{ lang }}\"><body>"
            "CUSTOM-LAYOUT {{ site_title }}"
            "{% include 'partials/marker.html' %}"
            "{% block content %}{% endblock %}"
            "</body></html>",
            encoding="utf-8")
        (custom / "partials" / "marker.html").write_text("MARKER-OK", encoding="utf-8")
        (custom / "home.html").write_text(
            "{% extends 'base.html' %}{% block content %}HOME-CUSTOM{% endblock %}",
            encoding="utf-8")

        self._config["templates_dir"] = str(custom)
        reset_environment()
        try:
            page = self.app.request("GET", "/").text
            self.assertIn("CUSTOM-LAYOUT", page)
            self.assertIn("MARKER-OK", page)      # include 链也在新目录里解析
            self.assertIn("HOME-CUSTOM", page)
        finally:
            self._config["templates_dir"] = "elenvind/templates"
            reset_environment()

        # 换回默认目录后仍是原来的模板
        page = self.app.request("GET", "/").text
        self.assertNotIn("CUSTOM-LAYOUT", page)
        self.assertIn("header-brand", page)

    def test_intro_is_rendered_and_escaped(self):
        self._config["params"]["intro"] = "Hello <script>alert(1)</script>"
        page = self.app.request("GET", "/").text
        self.assertIn("Hello", page)
        self.assertNotIn("<script>alert(1)</script>", page)

        self._config["params"]["intro"] = ""
        self.assertNotIn('class="intro"', self.app.request("GET", "/").text)

    def test_social_and_project_entries_come_from_config(self):
        self._config["params"]["social"] = [
            {"name": "Mastodon", "url": "https://social.example/@me", "icon": "/i.svg"}
        ]
        self._config["params"]["projects"] = [
            {"name": "ProjX", "url": "https://proj.example", "description": "A project"}
        ]
        page = self.app.request("GET", "/").text
        self.assertIn("https://social.example/@me", page)
        self.assertIn("ProjX", page)
        self.assertIn("A project", page)

    def test_static_page_urls_come_from_config(self):
        self._config["custom_pages_dir"] = str(self.custom_pages_dir)
        self.write_page("cfgpage", "content from custom dir")
        page = self.app.request("GET", "/cfgpage").text
        self.assertIn("content from custom dir", page)

    def test_articles_dir_change_switches_content_source(self):
        from elenvind.features.blog import logic as blog

        self.write_article("default-post", "default body",
                           {"title": "Default", "date": "2026-01-01"})
        self.assertEqual([a["slug"] for a in blog.get_articles()], ["default-post"])

        alternate = self.tmpdir / "alternate-articles"
        alternate.mkdir()
        (alternate / "other-post.md").write_text(
            '+++\ntitle = "Other"\ndate = "2026-02-01"\n+++\n\nother body\n', encoding="utf-8")
        self._config["articles_dir"] = str(alternate)
        blog._articles_cache = None
        blog._file_stats = None
        self.assertEqual([a["slug"] for a in blog.get_articles()], ["other-post"])
        response = self.app.request("GET", "/article/other-post")
        self.assertEqual(response.status, 200)
        self.assertIn("other body", response.text)
        self.assertEqual(self.app.request("GET", "/article/default-post").status, 404)

    # ---------- Cookie ----------
    def test_cookie_prefix_switch_changes_set_read_delete(self):
        from elenvind.core import security
        from elenvind.core.config import apply_runtime_config

        for enabled, expected_name in ((False, "session"), (True, "__Host-session")):
            with self.subTest(prefix=enabled):
                self._config["server"]["cookie_prefix"] = enabled
                apply_runtime_config()
                try:
                    self.create_user(email=f"cp{enabled}@example.com")
                    login = self.login(f"cp{enabled}@example.com", "correct horse battery")
                    names = [entry.split("=", 1)[0]
                             for entry in login.headers_all("set-cookie")]
                    self.assertIn(expected_name, names)

                    session = self.app.set_cookie_value(login, expected_name)
                    self.assertTrue(session)
                    # 读取：用写入时的名字能取回同一会话
                    page = self.app.request("GET", "/user", cookies={expected_name: session})
                    self.assertIn(f"cp{enabled}@example.com", page.text)

                    # 删除：清理头覆盖同一个名字，且立即过期
                    csrf = self.fetch_csrf()
                    logout = self.app.request("POST", "/logout",
                                              form={"csrf_token": csrf},
                                              cookies={expected_name: session, "csrf": csrf})
                    self.assertEqual(logout.status, 302)
                    clear_headers = [entry for entry in logout.headers_all("set-cookie")
                                     if entry.split("=", 1)[0] == expected_name]
                    self.assertTrue(clear_headers,
                                    f"no clearing Set-Cookie for {expected_name}")
                    self.assertIn("max-age=0", clear_headers[0].lower())
                    # 会话确实失效：再用它请求个人中心只剩未登录页
                    stale = self.app.request("GET", "/user", cookies={expected_name: session})
                    self.assertNotIn(f"cp{enabled}@example.com", stale.text)
                finally:
                    self._config["server"]["cookie_prefix"] = False
                    apply_runtime_config()
        self.assertEqual(security.cookie_name("session"), "session")

    # ---------- 代理与客户端 IP ----------
    def test_trusted_proxies_control_client_ip(self):
        from elenvind.core.utils import get_client_ip

        def scope(peer, forwarded=None):
            headers = []
            if forwarded:
                headers.append((b"x-forwarded-for", forwarded.encode()))
            return {"client": (peer, 1234), "headers": headers}

        self._config["server"]["trusted_proxies"] = ["127.0.0.1", "::1"]
        self.assertEqual(get_client_ip(scope("127.0.0.1", "1.2.3.4")), "1.2.3.4")
        self.assertEqual(get_client_ip(scope("203.0.113.9", "1.2.3.4")), "203.0.113.9")

        # 把代理换成另一个地址：新的可信对端生效，旧的恢复为"不可信"
        self._config["server"]["trusted_proxies"] = ["10.0.0.5"]
        self.assertEqual(get_client_ip(scope("10.0.0.5", "9.9.9.9")), "9.9.9.9")
        self.assertEqual(get_client_ip(scope("127.0.0.1", "9.9.9.9")), "127.0.0.1")

        # 空列表 = 谁都不信
        self._config["server"]["trusted_proxies"] = []
        self.assertEqual(get_client_ip(scope("127.0.0.1", "9.9.9.9")), "127.0.0.1")


class DatabasePathConfigTests(unittest.TestCase):
    """database 键必须真正控制 SQLite 路径，优先级必须与文档一致。"""

    def setUp(self):
        self._original_path = db_base.DB_PATH
        self._original_env = os.environ.pop("ELENVIND_DB", None)
        self._original_config = dict(_config_dict())

    def tearDown(self):
        db_base.DB_PATH = self._original_path
        if self._original_env is None:
            os.environ.pop("ELENVIND_DB", None)
        else:
            os.environ["ELENVIND_DB"] = self._original_env
        live = _config_dict()
        live.clear()
        live.update(self._original_config)

    def test_config_database_key_sets_path(self):
        live = _config_dict()
        live["database"] = "custom.db"
        db_base.apply_db_path()
        self.assertEqual(Path(db_base.DB_PATH), ROOT / "custom.db")

    def test_relative_database_resolves_against_project_root(self):
        live = _config_dict()
        live["database"] = "data/nested/custom.db"
        db_base.apply_db_path()
        self.assertEqual(Path(db_base.DB_PATH), ROOT / "data" / "nested" / "custom.db")

    def test_environment_variable_wins_over_config(self):
        live = _config_dict()
        live["database"] = "from-config.db"
        os.environ["ELENVIND_DB"] = str(ROOT / "from-env.db")
        db_base.DB_PATH = ROOT / "sentinel.db"
        db_base.apply_db_path()
        self.assertEqual(Path(db_base.DB_PATH), ROOT / "from-env.db")

    def test_blank_config_falls_back_to_default(self):
        live = _config_dict()
        live["database"] = ""
        # 测试夹具会设置 ELENVIND_DB 兜底；这里要验证的是"无环境变量时"的行为
        os.environ.pop("ELENVIND_DB", None)
        db_base.DB_PATH = ROOT / "sentinel.db"
        db_base.apply_db_path()
        self.assertEqual(Path(db_base.DB_PATH), db_base.DEFAULT_DB_PATH)

    def test_blank_config_also_covers_whitespace_only(self):
        live = _config_dict()
        live["database"] = "   "
        os.environ.pop("ELENVIND_DB", None)
        db_base.DB_PATH = ROOT / "sentinel.db"
        db_base.apply_db_path()
        self.assertEqual(Path(db_base.DB_PATH), db_base.DEFAULT_DB_PATH)

    def test_env_absence_does_not_clobber_an_already_applied_path(self):
        """环境变量不存在且配置合法时，路径来自 config（不是启动时读到的 env）。"""
        live = _config_dict()
        live["database"] = "configured-only.db"
        os.environ.pop("ELENVIND_DB", None)
        db_base.apply_db_path()
        self.assertEqual(Path(db_base.DB_PATH), ROOT / "configured-only.db")


class LoggingPathConfigTests(unittest.TestCase):
    def test_logging_file_resolves_against_project_root(self):
        from elenvind.core.logging_config import resolve_log_path

        self.assertEqual(resolve_log_path("logs/app.log"), ROOT / "logs" / "app.log")
        self.assertEqual(resolve_log_path(""), ROOT / "logs" / "app.log")
        absolute = ROOT / "somewhere" / "other.log"
        self.assertEqual(resolve_log_path(str(absolute)), absolute)


class DeadConfigurationGuardTests(unittest.TestCase):
    """config.example.toml 里的每个键都必须被 runtime 读取（无死配置）。"""

    #: 明确标记为"仅供文档/示例展示"的键（当前为空）
    DOCUMENTATION_ONLY = set()

    def _flatten(self, data, prefix=""):
        keys = []
        for key, value in data.items():
            path = f"{prefix}{key}"
            keys.append(path)
            if isinstance(value, dict):
                keys.extend(self._flatten(value, prefix + key + "."))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                keys.extend(self._flatten(value[0], prefix + key + "[]."))
        return keys

    def _runtime_source(self):
        """整个 elenvind 包（含 core/ 与 features/）的源码，用于死配置扫描。"""
        parts = []
        for path in sorted((PROJECT_ROOT / "elenvind").rglob("*.py")):
            parts.append(path.read_text(encoding="utf-8"))
        return "\n".join(parts)

    def test_every_example_config_key_is_read(self):
        with open(PROJECT_ROOT / "config.example.toml", "rb") as handle:
            data = tomllib.load(handle)
        source = self._runtime_source()
        dead = []
        for key in self._flatten(data):
            if key in self.DOCUMENTATION_ONLY:
                continue
            leaf = key.split(".")[-1].replace("[]", "")
            if not re.search(rf'["\']{re.escape(leaf)}["\']', source):
                dead.append(key)
        self.assertEqual(dead, [], f"dead configuration keys: {dead}")

    def test_no_leftover_params_author(self):
        """params.author 已被移除（曾是死配置）。

        只针对 `params` 命名空间断言：评论行里的 `"author"` 字段是无关概念，
        不应被这条守卫误伤。
        """
        source = self._runtime_source()
        for pattern in ('params.get("author")', "params['author']",
                        'params["author"]'):
            self.assertNotIn(pattern, source)
        for name in ("config.toml", "config.example.toml"):
            text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("params.author", text)
            with open(PROJECT_ROOT / name, "rb") as handle:
                data = tomllib.load(handle)
            self.assertNotIn("author", data.get("params", {}))

    def test_documented_keys_exist_in_example(self):
        """文档里列出的配置键必须在示例配置里真实存在。"""
        docs = (PROJECT_ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        with open(PROJECT_ROOT / "config.example.toml", "rb") as handle:
            data = tomllib.load(handle)
        flat = set(self._flatten(data))
        leaves = {key.split(".")[-1].replace("[]", "") for key in flat}
        documented = set(re.findall(r"^\| `([a-z_]+)`", docs, re.M))
        missing = sorted(name for name in documented if name not in leaves)
        self.assertEqual(missing, [], f"documented but missing from config.example.toml: {missing}")


class NoUnclosedConnectionGuardTests(unittest.TestCase):
    """守卫：db_*.py 里每个 get_connection() 都必须被 closing(...) 或 try/finally 兜住。"""

    def test_every_connection_is_closed_on_all_paths(self):
        offenders = []
        for path in sorted((PROJECT_ROOT / "elenvind").rglob("db_*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, str(path))
            for func in [node for node in ast.walk(tree)
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                covered = set()
                for node in ast.walk(func):
                    if isinstance(node, ast.With):
                        expression = ast.unparse(node.items[0].context_expr)
                        if "closing" in expression:
                            covered.update(child.lineno for child in ast.walk(node)
                                           if hasattr(child, "lineno"))
                has_try_finally = any(isinstance(node, ast.Try) and node.finalbody
                                      for node in ast.walk(func))
                for node in ast.walk(func):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                            and node.func.id == "get_connection":
                        if node.lineno not in covered and not has_try_finally:
                            offenders.append(f"{path.name}:{func.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"get_connection() without guaranteed close: {offenders}")

    def test_no_duplicate_top_level_definitions(self):
        """同一模块内不得有重名顶层定义（同步/合并事故的典型残留）。"""
        duplicates = []
        for path in sorted((PROJECT_ROOT / "elenvind").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            seen = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen:
                        duplicates.append(f"{path.name}:{node.name} "
                                          f"(lines {seen[node.name]} and {node.lineno})")
                    seen[node.name] = node.lineno
        self.assertEqual(duplicates, [], f"duplicate top-level definitions: {duplicates}")


def _config_dict():
    from elenvind.core.config import config
    return config


if __name__ == "__main__":
    unittest.main()
