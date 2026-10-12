"""运维边界：备份、限流响应成本、迁移失败与部署文件的机械校验。

外部工具（systemd-analyze / nginx / openssl）在缺少时自动跳过；这类校验的价值
在于**防止文档里的写法回归**（例如 systemd 单元行尾注释会被静默忽略）。
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import support
from tests.support import ARTICLE_SLUG, PROJECT_ROOT

SERVICE_FILE = PROJECT_ROOT / "deploy" / "elenvind.service"
NGINX_EXAMPLE = PROJECT_ROOT / "deploy" / "nginx.conf.example"


class Backup(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()
        support.Session().create_account("Backup", "backup@example.test", "correct-horse-1")
        self.directory = Path(support.WORKSPACE.root) / "backups"
        self.directory.mkdir(exist_ok=True)

    def test_backup_is_consistent_and_private(self):
        from elenvind import db

        target = self.directory / "unit.db"
        written = db.backup_to(target)
        self.assertEqual(written, target)
        # 备份含用户 / 会话 / 登录流水：必须不受调用方 umask 影响
        self.assertEqual(oct(target.stat().st_mode & 0o777), oct(0o600))
        conn = sqlite3.connect(target)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             db.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_refuses_to_overwrite_an_existing_backup(self):
        from elenvind import db

        target = self.directory / "again.db"
        db.backup_to(target)
        with self.assertRaises(db.BackupError):
            db.backup_to(target)

    def test_failed_backup_never_touches_the_existing_copy(self):
        """备份失败绝不能破坏已有的有效备份（逐字节比对）。"""
        import hashlib

        from elenvind import db

        target = self.directory / "keep.db"
        db.backup_to(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.assertRaises(db.BackupError):
            db.backup_to(target)                     # 目标已存在 → 直接拒绝
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest)

    def test_restored_database_supports_reads_and_writes(self):
        """恢复后的库必须能正常读写（走应用层真实路径，而不只是 PRAGMA）。"""
        import shutil

        from elenvind import db

        support.Session().create_account("Restore", "restore@example.test", "correct-horse-1")
        backup = self.directory / "restore.db"
        db.backup_to(backup)

        restored = Path(support.WORKSPACE.root) / "restored.db"
        shutil.copyfile(backup, restored)            # 模拟"把备份放回去"
        original = db.DB_PATH
        try:
            db.configure(restored)
            self.assertIsNotNone(db.get_user_by_email("restore@example.test"))   # 读
            user_id = db.create_user("After", "after@example.test", "hash")      # 写
            self.assertIsNotNone(db.get_user_by_id(user_id))
        finally:
            db.configure(original)                   # 别影响其它用例

    def test_missing_database_is_reported(self):
        from elenvind import db

        with self.assertRaises(db.BackupError):
            db.backup_to(self.directory / "x.db",
                         db_path=Path(support.WORKSPACE.root) / "does-not-exist.db")


class RateLimitResponseCost(unittest.TestCase):
    """被限流的请求不该渲染整页 —— 否则"达到限制"本身就成了放大器。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        config = support.live_config()
        self._saved = config["comment_limits"]
        config["comment_limits"] = {"max_per_user": 1, "max_per_ip": 100,
                                    "window_seconds": 60}
        self.user = support.Session()
        self.user.create_account("Lim", "lim@example.test", "correct-horse-1")

    def tearDown(self):
        support.live_config()["comment_limits"] = self._saved

    def test_rate_limited_response_is_lightweight(self):
        marker = "COMMENT_MARKER_XYZ"
        self.assertEqual(self.user.comment(ARTICLE_SLUG, marker).status, 302)
        limited = self.user.comment(ARTICLE_SLUG, "second")
        article = self.user.get(f"/article/{ARTICLE_SLUG}")
        self.assertEqual(limited.status, 429)
        self.assertLess(len(limited.body), len(article.body) * 0.5,
                        "the 429 must not render the whole article page")
        self.assertNotIn(marker.encode(), limited.body,
                         "the 429 must not include the comment tree")


class RegisterPreCheck(unittest.TestCase):
    """被限流的公开注册必须**不再**碰全局写锁。

    机械证明：把 `db.auth.write_tx` 换成会爆炸的替身 —— 如果预检没生效、
    超限请求仍然进入写事务，测试会以内层 `AssertionError` 失败。
    """

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        from elenvind import db

        self.db = db
        config = support.live_config()
        self._saved = config["register_limits"]
        config["register_limits"] = {"max_per_ip": 2, "window_seconds": 3600}
        for _ in range(2):                     # 用真实记账函数把该 IP 填满
            db.auth.try_register_attempt("127.0.0.1", max_per_ip=2, window_seconds=3600)

    def tearDown(self):
        support.live_config()["register_limits"] = self._saved

    def test_saturated_ip_is_rejected_without_touching_the_write_lock(self):
        from unittest import mock

        with mock.patch("elenvind.db.auth.write_tx",
                        side_effect=AssertionError("pre-check must skip the write lock")):
            result = support.Session().register("Over", "over@example.test", "correct-horse-1")
        self.assertEqual(result.status, 200)
        self.assertIn("too many", result.text().lower())

    def test_ip_below_the_limit_can_still_register(self):
        support.reset_database()               # 清空窗口：预检不得误杀正常注册
        self.assertEqual(
            support.Session().register("Ok", "ok@example.test", "correct-horse-1").status, 302)


class MigrationSafety(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()

    def test_init_db_is_idempotent(self):
        from elenvind import db

        db.init_db()
        db.init_db()
        self.assertEqual(support.query("PRAGMA user_version")[0][0], db.SCHEMA_VERSION)

    def test_leftover_legacy_table_blocks_startup(self):
        from elenvind import db
        from elenvind.db.migration import MigrationError

        with db.write_tx() as conn:
            conn.execute("CREATE TABLE comment_legacy (id INTEGER)")
        try:
            with self.assertRaises(MigrationError):
                db.init_db()
        finally:
            with db.write_tx() as conn:        # 收拾干净，别影响其它用例
                conn.execute("DROP TABLE comment_legacy")


class LogRotation(unittest.TestCase):
    """日志被外部工具（logrotate）改名后，应用必须能继续写。

    `[logging].rotate = false` 时用的是 `WatchedFileHandler`：它在每次写之前
    检查 dev/ino，发现文件被换掉就重新打开 —— 这正是多 worker 部署下唯一安全
    的做法。这里用真实的 handler 走一遍：写 → 改名 → 再写。
    """

    def setUp(self):
        support.ensure_application()

    def tearDown(self):
        # 恢复测试夹具原本的日志目标，别影响其它用例对日志内容的断言
        from elenvind.core.logging_config import setup_logging

        config = support.live_config()["logging"]
        setup_logging(level=config["level"], log_file=config["file"],
                      max_bytes=config["max_bytes"], backup_count=config["backup_count"],
                      rotate=config.get("rotate", False))

    def test_handler_reopens_the_log_after_external_rotation(self):
        import logging

        from elenvind.core.logging_config import setup_logging

        path = Path(support.WORKSPACE.root) / "rotation.log"
        setup_logging(level="info", log_file=str(path), rotate=False)
        logger = logging.getLogger("elenvind.tests.rotation")
        try:
            logger.info("before-rotation")
            self._flush()
            self.assertIn("before-rotation", path.read_text(encoding="utf-8"))

            path.rename(path.with_suffix(".log.1"))          # 模拟 logrotate
            logger.info("after-rotation")
            self._flush()

            self.assertTrue(path.exists(), "the log file must be recreated")
            self.assertIn("after-rotation", path.read_text(encoding="utf-8"))
        finally:
            for handler in logging.getLogger().handlers:
                handler.flush()

    @staticmethod
    def _flush() -> None:
        import logging

        for handler in logging.getLogger().handlers:
            handler.flush()


class DeploymentFiles(unittest.TestCase):
    """部署文件的机械校验（能跑外部工具就跑，跑不了退化为静态断言）。"""

    def _nginx_directive(self, name: str) -> str:
        text = NGINX_EXAMPLE.read_text(encoding="utf-8")
        line = next((item for item in text.splitlines() if item.strip().startswith(name)),
                    None)
        self.assertIsNotNone(line, f"{name} is missing from deploy/nginx.conf.example")
        return line.split(None, 1)[1].strip().rstrip(";")

    @staticmethod
    def _to_bytes(value: str) -> int:
        suffixes = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
        text = value.strip().lower()
        if text[-1] in suffixes:
            return int(float(text[:-1]) * suffixes[text[-1]])
        return int(text)

    def test_nginx_body_limit_leaves_room_for_the_application_limit(self):
        """请求体上限必须"应用先触发、Nginx 兜底"。

        若 Nginx 的上限 ≤ 应用上限，Nginx 会抢先返回 413，应用自己的 413
        （有日志、有测试）在反代后面永远观察不到 —— 这条守的就是那个顺序。
        """
        import tomllib

        with open(PROJECT_ROOT / "config.example.toml", "rb") as handle:
            example = tomllib.load(handle)
        application_limit = int(example["max_body_size"])
        nginx_limit = self._to_bytes(self._nginx_directive("client_max_body_size"))
        self.assertGreater(nginx_limit, application_limit,
                           f"nginx {nginx_limit} must exceed the application limit "
                           f"{application_limit}")

    def test_nginx_proxy_read_timeout_exceeds_gunicorn_timeout(self):
        """代理读超时必须大于 Gunicorn 的 worker 超时（默认 30s），否则两边同时超时。"""
        timeout = int(self._nginx_directive("proxy_read_timeout").rstrip("s"))
        self.assertGreater(timeout, 30, "proxy_read_timeout must exceed gunicorn's 30s")

    def test_systemd_unit_has_no_trailing_comments(self):
        """systemd **不**把行尾 `#` 当注释：整行会被并入参数值并被静默忽略。"""
        offenders = []
        for number, line in enumerate(SERVICE_FILE.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "#" in stripped:
                offenders.append((number, stripped))
        self.assertEqual(offenders, [], f"trailing comments in a systemd unit: {offenders}")

    def test_read_write_paths_are_narrow(self):
        """服务账号不得拥有整个项目树的写权限（代码/模板/配置必须只读）。"""
        line = next(text for text in SERVICE_FILE.read_text(encoding="utf-8").splitlines()
                    if text.startswith("ReadWritePaths="))
        paths = [item.lstrip("-") for item in line.split("=", 1)[1].split()]
        self.assertNotIn("/srv/elenvind", paths,
                         "the whole project tree must not be writable by the service")
        for expected in ("/srv/elenvind/data", "/srv/elenvind/logs", "/srv/elenvind/backups"):
            self.assertIn(expected, paths)

    def test_shipped_default_database_lives_in_the_writable_subdirectory(self):
        """`config.example.toml` 的默认库路径必须落在 systemd 允许写的目录里。"""
        import tomllib

        with open(PROJECT_ROOT / "config.example.toml", "rb") as handle:
            example = tomllib.load(handle)
        self.assertTrue(str(example.get("database", "")).startswith("data/"),
                        f"database must default to data/: {example.get('database')!r}")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_systemd_analyze_verify_reports_no_ignored_directives(self):
        result = subprocess.run(["systemd-analyze", "verify", str(SERVICE_FILE)],
                                capture_output=True, text=True, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        ignored = [line for line in output.splitlines()
                   if ("ignoring" in line or "Invalid argument" in line)
                   and "not executable" not in line]      # 本机没有 /srv/elenvind 属正常
        self.assertEqual(ignored, [], f"systemd ignored directives: {ignored}")

    @unittest.skipUnless(shutil.which("nginx") and shutil.which("openssl"),
                         "nginx/openssl are unavailable")
    def test_nginx_example_parses(self):
        """示例必须能在 `http {}` 上下文里被 nginx 接受（含 limit_req_zone / http2 on）。"""
        example = NGINX_EXAMPLE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            certificate = workspace / "cert.pem"
            key = workspace / "key.pem"
            generated = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(certificate), "-days", "1",
                 "-subj", "/CN=example.com"],
                capture_output=True, text=True, check=False)
            if generated.returncode != 0:                  # 证书生成失败则跳过而非误报
                self.skipTest(f"openssl could not generate a test certificate: {generated.stderr}")
            site = (example
                    .replace("/etc/letsencrypt/live/example.com/fullchain.pem", str(certificate))
                    .replace("/etc/letsencrypt/live/example.com/privkey.pem", str(key))
                    .replace("/var/log/nginx/elenvind.access.log", str(workspace / "access.log"))
                    .replace("/var/log/nginx/elenvind.error.log", str(workspace / "error.log")))
            (workspace / "site.conf").write_text(site, encoding="utf-8")
            (workspace / "nginx.conf").write_text(
                "worker_processes 1;\n"
                f"error_log {workspace / 'error.log'};\n"
                f"pid {workspace / 'nginx.pid'};\n"
                "events { worker_connections 64; }\n"
                "http {\n"
                "  access_log off;\n"
                f"  include {workspace / 'site.conf'};\n"
                "}\n", encoding="utf-8")
            result = subprocess.run(
                ["nginx", "-t", "-c", str(workspace / "nginx.conf"), "-p", str(workspace)],
                capture_output=True, text=True, check=False)
            output = (result.stdout or "") + (result.stderr or "")
            # 非特权端口 bind 失败属环境限制；这里只要求语法/上下文被接受
            self.assertIn("syntax is ok", output, output)


if __name__ == "__main__":
    unittest.main()
