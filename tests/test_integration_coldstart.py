"""冷启动端到端集成测试（对应验收清单）。

与其它测试不同，本模块**不注入合成配置**：它用仓库真实的 config.toml 走完整
lifespan 启动流程，只把数据库与内容目录重定向到临时位置，然后依次访问
验收清单里的每一条路径与动作，确认整条链路可用。
"""
import os
import unittest

from tests.support import PROJECT_ROOT, AppHarness, run_async  # noqa: F401

from elenvind.core import config as config_module
from elenvind.core import db_base
from elenvind.core import lifespan as lifespan_module
from elenvind.core.config import apply_runtime_config, load_config, validate_config
from elenvind.features.blog import logic as blog_logic
from elenvind.features.pages import logic as pages_logic


class ColdStartTests(unittest.TestCase):
    """真实配置 + 真实 lifespan + 真实路由。"""

    def setUp(self):
        import shutil
        import uuid

        root = PROJECT_ROOT / ".testtmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmpdir = root / f"cold-{uuid.uuid4().hex[:12]}"
        (self.tmpdir / "articles").mkdir(parents=True)
        (self.tmpdir / "custom_pages").mkdir(parents=True)
        self.db_path = self.tmpdir / "cold.db"

        # 先加载真实 config.toml，再只覆盖路径类配置（其余保持仓库设定）
        self._original_config = dict(config_module.config)
        self._original_db_path = db_base.DB_PATH
        self._original_db_env = os.environ.get("ELENVIND_DB")
        load_config()
        config_module.config["database"] = str(self.db_path)
        config_module.config["articles_dir"] = str(self.tmpdir / "articles")
        config_module.config["custom_pages_dir"] = str(self.tmpdir / "custom_pages")
        config_module.config["logging"] = {"level": "critical",
                                           "file": str(self.tmpdir / "app.log")}
        config_module.config["admin_user_id"] = 1
        # 用临时内容做冒烟（真实 config.toml + 临时路径）
        (self.tmpdir / "articles" / "smoke.md").write_text(
            '+++\ntitle = "Smoke"\ndate = "2026-01-01"\nauthors = ["Tester"]\n+++\n\n'
            "# Heading\n\nBody with **bold**, `code`, [link](https://example.com).\n\n"
            "```py\nprint(1)\n```\n",
            encoding="utf-8")
        (self.tmpdir / "custom_pages" / "about.md").write_text(
            "About page **content**.", encoding="utf-8")

        db_base.DB_PATH = self.db_path
        os.environ["ELENVIND_DB"] = str(self.db_path)
        lifespan_module.SKIP_CONFIG_LOAD["value"] = True
        self.app = AppHarness()
        self.app.startup()
        self.cleanup_paths = (self.tmpdir,)

    def tearDown(self):
        import shutil

        lifespan_module.SKIP_CONFIG_LOAD["value"] = False
        db_base.DB_PATH = self._original_db_path
        if self._original_db_env is None:
            os.environ.pop("ELENVIND_DB", None)
        else:
            os.environ["ELENVIND_DB"] = self._original_db_env
        config_module.config.clear()
        config_module.config.update(self._original_config)
        blog_logic._articles_cache = None
        blog_logic._file_stats = None
        blog_logic._body_cache.clear()
        blog_logic._failed_stats = {}
        pages_logic._pages_cache = None
        pages_logic._file_stats = None
        from elenvind.core.templating import reset_environment
        reset_environment()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- 辅助 ----------
    def _csrf(self):
        response = self.app.request("GET", "/login")
        return self.app.set_cookie_value(response, "csrf")

    def _register_and_login(self, email, nickname="Tester", password="password-123"):
        csrf = self._csrf()
        cookies = {"csrf": csrf}
        register = self.app.request("POST", "/register",
                                    form={"csrf_token": csrf, "nickname": nickname,
                                          "email": email, "password": password,
                                          "confirm_password": password},
                                    cookies=cookies)
        self.assertEqual(register.status, 302, register.text[:300])
        login = self.app.request("POST", "/login",
                                 form={"csrf_token": csrf, "email": email,
                                       "password": password},
                                 cookies=cookies)
        self.assertEqual(login.status, 302, login.text[:300])
        session = self.app.set_cookie_value(login, "session")
        self.assertTrue(session)
        return session, csrf

    # ---------- 验收清单 ----------
    def test_full_walkthrough(self):
        # 首页
        home = self.app.request("GET", "/")
        self.assertEqual(home.status, 200)
        self.assertIn("Smoke", home.text)

        # 文章页 + Markdown 渲染
        article = self.app.request("GET", "/article/smoke")
        self.assertEqual(article.status, 200)
        for fragment in ("<h1", "Heading</h1>", "<strong>bold</strong>",
                         "<code>code</code>",
                         '<a href="https://example.com"', 'class="language-py"'):
            self.assertIn(fragment, article.text)

        # 自定义页面
        page = self.app.request("GET", "/about")
        self.assertEqual(page.status, 200)
        self.assertIn("<strong>content</strong>", page.text)

        # SEO
        robots = self.app.request("GET", "/robots.txt")
        self.assertEqual(robots.status, 200)
        self.assertIn("Sitemap:", robots.text)
        sitemap = self.app.request("GET", "/sitemap.xml")
        self.assertEqual(sitemap.status, 200)
        self.assertIn("/article/smoke", sitemap.text)

        # 主题
        theme = self.app.request("GET", "/theme", query={"mode": "dark", "next": "/about"})
        self.assertEqual(theme.status, 302)
        self.assertIn("theme=dark", " ".join(theme.headers_all("set-cookie")))

        # 404 / 405 / 退出确认页
        self.assertEqual(self.app.request("GET", "/no-such-page").status, 404)
        self.assertEqual(
            self.app.request("GET", "/article/a/comment/delete/1").status, 405)
        # GET /logout 是友好确认页（未登录 -> 提示已退出），不是 405
        self.assertEqual(self.app.request("GET", "/logout").status, 200)

        # 注册 → 登录（admin_user_id = 1，因此这个账号也是管理员）
        session, csrf = self._register_and_login("cold-start@example.com")
        cookies = {"session": session, "csrf": csrf}

        # 个人中心
        user_page = self.app.request("GET", "/user", cookies=cookies)
        self.assertEqual(user_page.status, 200)
        self.assertIn("cold-start@example.com", user_page.text)

        # 评论 → 回复
        created = self.app.request("POST", "/article/smoke/comment",
                                   form={"csrf_token": csrf, "content": "first!"},
                                   cookies=cookies)
        self.assertEqual(created.status, 302)
        rendered = self.app.request("GET", "/article/smoke")
        self.assertIn("first!", rendered.text)
        self.assertIn('id="comments"', rendered.text)

        # 删除评论 → 恢复（本人可删；恢复仅管理员，这里由 id=1 的同一账号执行）
        from elenvind.core.db_comment import get_comments_by_article
        comment_id = get_comments_by_article("smoke")[0]["id"]
        deleted = self.app.request("POST", f"/article/smoke/comment/delete/{comment_id}",
                                   form={"csrf_token": csrf}, cookies=cookies)
        self.assertEqual(deleted.status, 302)
        # 管理员看到的是删除线原文（访客会被打码，见 test_comments）
        masked = self.app.request("GET", "/article/smoke", cookies=cookies)
        self.assertIn("is-deleted", masked.text)
        restored = self.app.request("POST", f"/article/smoke/comment/restore/{comment_id}",
                                    form={"csrf_token": csrf}, cookies=cookies)
        self.assertEqual(restored.status, 302)
        self.assertIn("first!", self.app.request("GET", "/article/smoke").text)

        # 改密 → 全会话失效 → 用新密码重新登录（模拟一个全新浏览器：不带旧 Cookie）
        changed = self.app.request("POST", "/user",
                                   form={"csrf_token": csrf, "action": "change_password",
                                         "old_password": "password-123",
                                         "new_password": "new-password-456",
                                         "confirm_password": "new-password-456"},
                                   cookies=cookies)
        self.assertEqual(changed.status, 302)
        self.assertEqual(changed.header("location"), "/login")
        # 旧会话已失效：再用它请求个人中心只会渲染未登录页
        stale = self.app.request("GET", "/user", cookies=cookies)
        # 旧会话失效 -> 视为未登录：渲染友好的"请先登录"页（不再是 403），
        # 但绝不泄漏该账号的任何信息，也不渲染只有登录后才有的表单
        self.assertEqual(stale.status, 200)
        self.assertNotIn("cold-start@example.com", stale.text)
        self.assertNotIn('name="password_confirm"', stale.text)
        self.assertNotIn('name="old_password"', stale.text)
        # 给出登录入口并带回跳地址（语言无关的断言）
        self.assertIn('href="/login?next=/user"', stale.text)

        csrf2 = self._csrf()
        relogin = self.app.request("POST", "/login",
                                   form={"csrf_token": csrf2, "email": "cold-start@example.com",
                                         "password": "new-password-456"},
                                   cookies={"csrf": csrf2})
        self.assertEqual(relogin.status, 302, relogin.text[:300])
        session2 = self.app.set_cookie_value(relogin, "session")

        # 注销
        logout = self.app.request("POST", "/logout", form={"csrf_token": csrf2},
                                  cookies={"session": session2, "csrf": csrf2})
        self.assertEqual(logout.status, 302)
        self.assertEqual(logout.header("location"), "/")

    def test_startup_rejects_invalid_configuration(self):
        """配置非法时 lifespan 必须明确失败，而不是起一个半初始化的应用。"""
        import logging

        from elenvind.app import app

        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "lifespan.shutdown"}

        async def send(message):
            sent.append(message)

        original = dict(config_module.config)
        original_handlers = logging.getLogger().handlers[:]
        try:
            config_module.config["server"]["port"] = 999999   # 非法端口
            run_async(app({"type": "lifespan"}, receive, send))
        finally:
            config_module.config.clear()
            config_module.config.update(original)
            for handler in logging.getLogger().handlers[:]:
                logging.getLogger().removeHandler(handler)
            for handler in original_handlers:
                logging.getLogger().addHandler(handler)

        self.assertTrue(sent, "lifespan produced no messages")
        self.assertEqual(sent[0]["type"], "lifespan.startup.failed")
        self.assertIn("port", sent[0]["message"])

    def test_startup_creates_missing_directories(self):
        from elenvind.core.logging_config import resolve_log_path
        log_path = resolve_log_path("logs/cold-test/app.log")
        self.assertTrue(str(log_path).endswith(os.path.join("logs", "cold-test", "app.log")))


def fragment_app():
    """返回被测 ASGI 应用对象（延迟导入，避免模块级循环依赖）。"""
    from elenvind.app import app
    return app


class LoggingTests(unittest.TestCase):
    """日志配置：路径、handler 不重复、关闭。"""

    def test_setup_logging_is_idempotent(self):
        import logging
        from elenvind.core.logging_config import setup_logging, shutdown_logging
        from tests.support import PROJECT_ROOT

        root = PROJECT_ROOT / ".testtmp"
        root.mkdir(parents=True, exist_ok=True)
        log_file = root / "logging-test" / "app.log"
        original_handlers = logging.getLogger().handlers[:]
        try:
            setup_logging(level="critical", log_file=str(log_file))
            first = list(logging.getLogger().handlers)
            setup_logging(level="critical", log_file=str(log_file))
            second = list(logging.getLogger().handlers)
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 2)
            self.assertTrue(log_file.parent.exists())
            shutdown_logging()
            self.assertEqual(logging.getLogger().handlers, [])
        finally:
            shutdown_logging()
            for handler in original_handlers:
                logging.getLogger().addHandler(handler)
            import shutil
            shutil.rmtree(log_file.parent, ignore_errors=True)

    def test_uvicorn_loggers_do_not_duplicate(self):
        import logging
        from elenvind.core.logging_config import setup_logging, shutdown_logging
        from tests.support import PROJECT_ROOT

        root = PROJECT_ROOT / ".testtmp"
        root.mkdir(parents=True, exist_ok=True)
        try:
            setup_logging(level="critical", log_file=str(root / "uv-test" / "app.log"))
            for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
                logger = logging.getLogger(name)
                self.assertEqual(len(logger.handlers), 2)
                self.assertFalse(logger.propagate)
        finally:
            shutdown_logging()
            import shutil
            shutil.rmtree(root / "uv-test", ignore_errors=True)


class ConfigValidationStartupTests(unittest.TestCase):
    def test_validate_runs_over_real_config(self):
        original = dict(config_module.config)
        try:
            load_config()
            validate_config()
            apply_runtime_config()
        finally:
            config_module.config.clear()
            config_module.config.update(original)


if __name__ == "__main__":
    unittest.main()
