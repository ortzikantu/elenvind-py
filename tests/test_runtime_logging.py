"""运行期日志测试：访问日志、写事务明细、启动细节、审计事件。

为什么单独一个文件：`tests/test_logging_proxy.py` 管的是"日志里**不许**出现什么"
（脱敏）与客户端 IP 信任；本文件管的是"日志里**必须**出现什么"（详细程度），
以及"详细化不能以泄漏为代价"。

覆盖：

| 日志 | 级别 | 断言 |
|---|---|---|
| `Request handled: METHOD path -> status in X ms (N bytes, ip=…, user=…, pid=…)` | INFO | 每请求一行、字段齐全、登录后带 user id、无查询串/Cookie/请求体、控制字符被转义 |
| `Write transaction committed: …` / `… slow: …` / `… rolled back …` | DEBUG / WARNING | 行数与时长为真、慢事务升级为 WARNING、回滚可见 |
| `Log file: …` / `Database: …` / `Startup complete in …` | INFO | 启动即可回答"库在哪、锁在哪、花了多久、哪个 pid" |
| 登录/注册/改密/删号/评论审核 | INFO/WARNING | 审计事件留痕，且不含密码/令牌/Cookie |
"""
import io
import logging
import time
import unittest

from tests.support import ElenvindTestCase

#: 复用脱敏清单：审计日志与访问日志都必须过同一道关
from tests.test_logging_proxy import SENSITIVE_MARKERS


class _CapturingHandler(logging.Handler):
    """把 root logger 上流过的记录攒起来（不影响其它 handler）。"""

    def __init__(self, level=logging.DEBUG):
        super().__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(record.getMessage() for record in self.records)

    def lines(self, needle):
        return [r.getMessage() for r in self.records
                if needle in r.getMessage()]

    def levels(self, needle):
        return {r.levelname for r in self.records if needle in r.getMessage()}


class _LogCaptureMixin:
    #: 抓的是应用自己的 logger。**不挂在 root 上**：`setup_logging()` 按设计会
    #: 摘掉 root 上的全部 handler（接管 gunicorn 的输出），挂 root 会在
    #: `startup()` 时被移除 —— 于是"启动日志"永远抓不到。
    APP_LOGGER = "elenvind"

    def capture_logs(self, level=logging.DEBUG):
        handler = _CapturingHandler(level)
        app_logger = logging.getLogger(self.APP_LOGGER)
        previous = app_logger.level
        app_logger.addHandler(handler)
        app_logger.setLevel(level)

        def restore():
            app_logger.removeHandler(handler)
            app_logger.setLevel(previous)

        self.addCleanup(restore)
        return handler

    def register_user(self, email, password, nickname):
        """走真实的 POST /register 流程（成功即 302）。"""
        token = self.fetch_csrf()
        response = self.app.request("POST", "/register",
                                    form={"csrf_token": token, "nickname": nickname,
                                          "email": email, "password": password,
                                          "confirm_password": password},
                                    cookies=self.csrf_cookies(token))
        self.assertEqual(response.status, 302, response.text[:400])
        return response


class AccessLogTests(_LogCaptureMixin, ElenvindTestCase):
    """每个请求一行访问日志（Core 负责，模块不需要写）。"""

    def test_one_access_line_per_request_with_the_expected_fields(self):
        logs = self.capture_logs()
        self.app.request("GET", "/definitely-missing")
        lines = logs.lines("Request handled:")
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertIn("GET /definitely-missing -> 404", line)
        self.assertIn(" ms ", line)
        self.assertIn(" bytes, ", line)
        self.assertIn("ip=127.0.0.1", line)
        self.assertIn("user=-", line, "匿名请求应记 user=-")
        self.assertIn("pid=", line, "多 worker 下必须能分辨是哪个进程")
        self.assertEqual(logs.levels("Request handled:"), {"INFO"})

    def test_authenticated_requests_log_the_user_id(self):
        self.write_article("logged-in", "body")
        self.register_user("access@example.com", "password-123", "Access")
        session, csrf = self.login_ok("access@example.com", "password-123")
        logs = self.capture_logs()
        self.app.request("GET", "/user", cookies=self.app_cookies(session, csrf))
        line = logs.lines("Request handled:")[-1]
        self.assertIn("GET /user -> 200", line)
        self.assertIn("user=1", line, line)

    def test_access_line_has_no_query_string_cookies_or_body(self):
        logs = self.capture_logs()
        self.app.request("GET", "/", query={"token": "SECRET-QUERY-VALUE"},
                         cookies={"session": "S" * 43, "csrf": "C" * 43})
        text = logs.text()
        for line in logs.lines("Request handled:"):
            self.assertNotIn("SECRET-QUERY-VALUE", line)
            self.assertNotIn("S" * 43, line)
            self.assertNotIn("C" * 43, line)
            self.assertNotIn("?", line, "只记 PATH_INFO，不记查询串")
        for marker in SENSITIVE_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_control_characters_in_the_path_are_escaped(self):
        """`PATH_INFO` 是 percent-decode 之后的：`%0a` 会变成真实换行。

        直接写日志就等于允许客户端伪造日志行（log injection），必须转义。
        """
        logs = self.capture_logs()
        self.app.request("GET", "/x\nFAKE 2026-01-01 ERROR injected\r\n")
        line = logs.lines("Request handled:")[0]
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertIn("\\x0a", line)
        self.assertIn("\\x0d", line)

    def test_static_assets_are_logged_too(self):
        logs = self.capture_logs()
        self.app.request("GET", "/css/style.css")
        lines = logs.lines("Request handled:")
        self.assertEqual(len(lines), 1)
        self.assertIn("GET /css/style.css -> 200", lines[0])

    def test_server_error_still_produces_an_access_line(self):
        from elenvind.app import app

        target = next(route for route in app.router.routes if route.path == "/")
        original = target.handler

        def boom(request):
            raise RuntimeError("boom")

        target.handler = boom
        try:
            logs = self.capture_logs()
            self.app.request("GET", "/")
        finally:
            target.handler = original
        self.assertIn("GET / -> 500", logs.lines("Request handled:")[0])


class WriteTransactionLogTests(_LogCaptureMixin, ElenvindTestCase):
    """写事务明细：正常提交 DEBUG，慢事务 WARNING，回滚留痕。"""

    def test_commit_logs_row_count_and_duration_at_debug(self):
        from elenvind import db
        from elenvind.db import connection as db_connection
        from elenvind.db import transaction as db_transaction

        logs = self.capture_logs()
        with db.write_tx() as conn:
            conn.execute("INSERT INTO user (nickname, email, password, created_at) "
                         "VALUES (?, ?, ?, ?)", ("Tx", "tx@example.com", "x", "2026-01-01"))
        lines = logs.lines("Write transaction committed:")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("1 row(s) changed", lines[0])
        self.assertIn("ms", lines[0])
        self.assertIn(str(db_connection.DB_PATH), lines[0], "要能看出写的是哪个库")
        self.assertEqual(logs.levels("Write transaction committed:"), {"DEBUG"})

    def test_slow_transaction_is_warned(self):
        from elenvind import db
        from elenvind.db import connection as db_connection
        from elenvind.db import transaction as db_transaction

        logs = self.capture_logs()
        original = db_transaction._SLOW_WRITE_SECONDS
        db_transaction._SLOW_WRITE_SECONDS = 0.01        # 不必真的睡 1 秒
        try:
            with db.write_tx() as conn:
                conn.execute("INSERT INTO user (nickname, email, password, created_at) "
                             "VALUES (?, ?, ?, ?)", ("Slow", "slow@example.com", "x", "2026-01-01"))
                time.sleep(0.05)
        finally:
            db_transaction._SLOW_WRITE_SECONDS = original
        self.assertEqual(logs.levels("Write transaction slow:"), {"WARNING"})
        self.assertNotIn("Write transaction committed:", logs.text())

    def test_rollback_is_logged_at_debug(self):
        from elenvind import db
        from elenvind.db import connection as db_connection
        from elenvind.db import transaction as db_transaction

        logs = self.capture_logs()
        with self.assertRaises(RuntimeError):
            with db.write_tx() as conn:
                conn.execute("INSERT INTO user (nickname, email, password, created_at) "
                             "VALUES (?, ?, ?, ?)", ("Gone", "gone@example.com", "x", "2026-01-01"))
                raise RuntimeError("business failure")
        lines = logs.lines("Write transaction rolled back after")
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(logs.levels("Write transaction rolled back after"), {"DEBUG"})


class StartupLogTests(_LogCaptureMixin, ElenvindTestCase):
    """启动日志要能回答：日志写哪儿、库在哪、锁在哪、起来花了多久。"""

    def test_debug_level_keeps_our_detail_but_clamps_third_party_noise(self):
        """`level = "debug"` 要看的是应用细节，不是 python-markdown 的扩展加载。"""
        from elenvind.core.logging_config import _NOISY_LOGGERS

        noisy = tuple(logging.getLogger(name) for name in _NOISY_LOGGERS)
        saved = {item.name: item.level for item in noisy}
        self.addCleanup(
            lambda: [logging.getLogger(name).setLevel(level)
                     for name, level in saved.items()])
        for item in noisy:
            item.setLevel(logging.DEBUG)

        self._config["logging"] = dict(self._config.get("logging", {}),
                                       level="debug")
        self.app.startup()

        self.assertEqual(logging.getLogger().level, logging.DEBUG,
                         "应用自身仍按 debug 输出")
        for item in noisy:
            with self.subTest(logger=item.name):
                self.assertGreaterEqual(item.level, logging.INFO)

    def test_startup_logs_log_file_database_and_completion(self):
        logs = self.capture_logs()
        self.app.startup()          # 幂等：夹具已经在 setUp 里跑过一次

        self.assertTrue(logs.lines("Log file:"), "缺少日志文件位置")
        database_lines = logs.lines("Database: ")
        self.assertEqual(len(database_lines), 1, database_lines)
        self.assertIn("journal_mode=", database_lines[0])
        self.assertIn("write lock", database_lines[0])
        self.assertIn(".write.lock", database_lines[0])

        complete = logs.lines("Startup complete in")
        self.assertEqual(len(complete), 1, complete)
        self.assertIn("pid=", complete[0])
        self.assertIn("startup hook(s)", complete[0])

    def test_startup_reports_every_hook_that_ran(self):
        logs = self.capture_logs()
        self.app.startup()
        from elenvind.app import STARTUP_HOOKS
        complete = logs.lines("Startup complete in")[0]
        # 装配层登记的钩子（文章索引 / 自定义页面）都真的跑过
        self.assertIn(f"{len(STARTUP_HOOKS)} startup hook(s)", complete)
        self.assertGreaterEqual(len(STARTUP_HOOKS), 2)


class AuditEventLogTests(_LogCaptureMixin, ElenvindTestCase):
    """业务审计事件：谁在什么时候做了会改状态的事。"""

    def _login(self, email, password):
        session, csrf = self.login_ok(email, password)
        return self.app_cookies(session, csrf)

    def test_authentication_events_are_logged(self):
        logs = self.capture_logs()
        self.register_user("audit@example.com", "password-123", "Audit")
        self.assertEqual(logs.levels("Registration succeeded: user_id=1"), {"INFO"})

        bad_token = self.fetch_csrf()
        self.app.request("POST", "/login",
                         form={"csrf_token": bad_token, "email": "audit@example.com",
                               "password": "wrong-password"},
                         cookies=self.csrf_cookies(bad_token))
        self.assertEqual(logs.levels("Login failed:"), {"INFO"})

        auth = self._login("audit@example.com", "password-123")
        self.assertEqual(logs.levels("Login succeeded: user_id=1"), {"INFO"})

        self.app.request("POST", "/logout",
                         form={"csrf_token": auth["csrf"]}, cookies=auth)
        self.assertEqual(logs.levels("Logout: user_id=1"), {"INFO"})

    def test_account_changes_are_logged_and_leave_no_secrets(self):
        self.register_user("change@example.com", "password-123", "Change")
        auth = self._login("change@example.com", "password-123")
        logs = self.capture_logs()

        self.app.request("POST", "/user",
                         form={"csrf_token": auth["csrf"], "action": "update_profile",
                               "nickname": "Renamed", "email": "change@example.com"},
                         cookies=auth)
        self.app.request("POST", "/user",
                         form={"csrf_token": auth["csrf"], "action": "change_password",
                               "old_password": "password-123",
                               "new_password": "password-456",
                               "confirm_password": "password-456"},
                         cookies=auth)

        self.assertEqual(logs.levels("Profile updated: user_id=1"), {"INFO"})
        self.assertEqual(logs.levels("Password changed: user_id=1"), {"INFO"})
        text = logs.text()
        for secret in ("password-123", "password-456"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)
        for marker in SENSITIVE_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_comment_events_are_logged(self):
        self.write_article("audit-post", "body")
        self.register_user("commenter@example.com", "password-123", "Commenter")
        auth = self._login("commenter@example.com", "password-123")
        logs = self.capture_logs()

        self.app.request("POST", "/article/audit-post/comment",
                         form={"csrf_token": auth["csrf"], "content": "hello"},
                         cookies=auth)
        created = logs.lines("Comment created: slug=audit-post")
        self.assertEqual(len(created), 1, created)

        comment_id = self._comment_id("audit-post")
        self.app.request("POST", f"/article/audit-post/comment/edit/{comment_id}",
                         form={"csrf_token": auth["csrf"], "content": "edited"},
                         cookies=auth)
        self.assertEqual(logs.levels(f"Comment edited: id={comment_id}"), {"INFO"})
        self.app.request("POST", f"/article/audit-post/comment/delete/{comment_id}",
                         form={"csrf_token": auth["csrf"]}, cookies=auth)
        # 删除 = 涂黑（不可逆）：日志只记录动作，不含正文
        self.assertEqual(logs.levels(f"Comment redacted: id={comment_id}"), {"INFO"})
        self.assertEqual(logs.lines("content="), [], "日志里不应出现评论正文")

    def test_rejected_login_is_a_warning_that_never_contains_the_email(self):
        self.register_user("limit@example.com", "password-123", "Limit")
        self._config["login_limits"] = dict(self._config.get("login_limits", {}),
                                            max_email_failures=1)
        logs = self.capture_logs()
        for _ in range(2):
            token = self.fetch_csrf()
            self.app.request("POST", "/login",
                             form={"csrf_token": token, "email": "limit@example.com",
                                   "password": "definitely-wrong"},
                             cookies=self.csrf_cookies(token))
        self.assertIn("WARNING", logs.levels("Login blocked (email failure limit)"))
        self.assertNotIn("limit@example.com", logs.text(),
                         "登录失败日志只记 IP，不记邮箱（PII）")

    # ---------- 便捷查询 ----------

    def _comment_id(self, slug):
        from elenvind.db.comment import get_comments_by_article
        return get_comments_by_article(slug)[0]["id"]


if __name__ == "__main__":
    unittest.main()
