"""C0 真实多进程测试：并发写、临界区互斥、崩溃恢复、迁移并发、锁路径隔离。

为什么必须是**独立进程**：被测的保证本身就是跨进程的（`fcntl.flock` 排他 +
SQLite 事务）。进程内 `threading.Lock` 或单进程多线程证明不了任何东西，
因此这里全部用 `multiprocessing`（fork）与真实文件数据库。

进程间同步一律用 `Event` / `Pipe` / `Queue`，**不靠 sleep 猜时机**；
唯一的等待时长断言（崩溃恢复必须在合理期限内完成）也是显式的超时上限。

C0 依赖 POSIX `fcntl.flock`，非 POSIX 平台整类跳过（实现会显式报错，
不会退化成"进程内假锁"）。
"""
import multiprocessing
import os
import queue
import signal
import sqlite3
import time
import unittest
from pathlib import Path

from tests.support import ElenvindTestCase

from elenvind.core import db_base
from elenvind.core.db_base import connect, lock_path_for, write_tx

try:                                    # fcntl 只在 POSIX 上存在
    import fcntl
except ImportError:                     # pragma: no cover - 非 POSIX 平台
    fcntl = None

POSIX = hasattr(os, "fork") and fcntl is not None
CTX = multiprocessing.get_context("fork") if POSIX else None

#: 崩溃恢复测试要写入足够多的数据，逼 SQLite 把未提交页刷进 WAL
#: （默认 `cache_size` 是 -2000，即约 2 MB；超过它才会 spill。
#: 实测 4 MB 的未提交 INSERT 会让 WAL 长到 ~2.4 MB，见下方断言。）
CRASH_BLOB_BYTES = 4 * 1024 * 1024
#: 未提交数据确实落到 WAL 的下限（用来确认这个崩溃测试是有效的）
WAL_MIN_SIZE = 1024 * 1024

LEGACY_SCHEMA_V1 = """
    CREATE TABLE user (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
        nickname_changed_at TEXT);
    CREATE TABLE comment (
        id INTEGER PRIMARY KEY AUTOINCREMENT, article_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL, content TEXT NOT NULL,
        created_at TEXT NOT NULL, parent_id INTEGER,
        is_deleted INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES user(id));
    CREATE TABLE session (
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES user(id) ON DELETE CASCADE);
    INSERT INTO user (nickname, email, password, created_at)
        VALUES ('Old','old@example.com','x','2026-01-01T00:00:00');
    INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
        VALUES ('post',1,'c1','2026-01-01T00:00:01',NULL);
    INSERT INTO comment (article_slug, user_id, content, created_at, parent_id)
        VALUES ('post',1,'c2','2026-01-01T00:00:02',1);
    PRAGMA user_version = 1;
"""


# ======================= 子进程目标（模块级，fork 后可调用） =======================
#
# 子进程不共享测试进程的 DB_PATH 语义，因此每个子进程入口都先调用
# `_child_setup()` 显式把数据库指到本次测试的临时文件上。


def _child_setup(db_path):
    os.environ["ELENVIND_DB"] = str(db_path)
    db_base.DB_PATH = Path(db_path)


def _prepare_tables():
    """建出 C0 测试专用的表（绝不碰生产 schema 语义）。

    必须在 `_child_setup()` 之后调用（用的是模块级 DB_PATH）。
    """
    with write_tx() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS c0_account "
                     "(id INTEGER PRIMARY KEY, tag TEXT UNIQUE)")
        conn.execute("CREATE TABLE IF NOT EXISTS c0_probe "
                     "(owner TEXT, entered_at REAL, left_at REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS c0_crash (marker TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS c0_blob (payload BLOB)")


def _child_prepare_tables(db_path, results):
    _child_setup(db_path)
    try:
        db_base.init_db()
        _prepare_tables()
        results.put(("ok", "prepared"))
    except Exception as error:                       # noqa: BLE001 - 上报给父进程
        results.put(("error", f"{type(error).__name__}: {error}"))


def _child_insert_batch(db_path, start, count, go, results):
    _child_setup(db_path)
    try:
        go.wait(60)
        for index in range(count):
            with write_tx() as conn:
                conn.execute("INSERT INTO c0_account (tag) VALUES (?)",
                             (f"tag-{start + index}",))
        results.put(("ok", count))
    except Exception as error:                       # noqa: BLE001
        results.put(("error", f"{type(error).__name__}: {error}"))


def _child_probe_section(db_path, owner, hold_seconds, attempting, entered, results):
    _child_setup(db_path)
    try:
        attempting.set()
        with write_tx() as conn:
            conn.execute(
                "INSERT INTO c0_probe (owner, entered_at, left_at) VALUES (?, ?, NULL)",
                (owner, time.monotonic()))
            entered.set()
            time.sleep(hold_seconds)
            conn.execute("UPDATE c0_probe SET left_at = ? WHERE owner = ?",
                         (time.monotonic(), owner))
        results.put(("ok", owner))
    except Exception as error:                       # noqa: BLE001
        results.put(("error", f"{owner}: {type(error).__name__}: {error}"))


def _child_crash_holder(db_path, pipe, marker, blob_bytes):
    """持有一个**未提交**的写事务，并把它写进 WAL，然后停在事务中途等被杀。

    `time.sleep` 不是"赌时机"：父进程只在收到 pipe 消息并确认 WAL 已有内容后
    才 SIGKILL，这里只是"停住不提交"。
    """
    _child_setup(db_path)
    with write_tx() as conn:
        conn.execute("INSERT INTO c0_crash (marker) VALUES (?)", (marker,))
        conn.execute("INSERT INTO c0_blob (payload) VALUES (?)",
                     (sqlite3.Binary(b"x" * blob_bytes),))
        pipe.send("in-transaction")
        time.sleep(300)              # 永远不提交：等着被 SIGKILL
    pipe.close()                     # pragma: no cover - 到不了


def _child_write_marker(db_path, tag, results):
    _child_setup(db_path)
    try:
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_account (tag) VALUES (?)", (tag,))
        results.put(("ok", tag))
    except Exception as error:                       # noqa: BLE001
        results.put(("error", f"{type(error).__name__}: {error}"))


def _child_read_count(db_path, results):
    _child_setup(db_path)
    try:
        with connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM c0_account").fetchone()[0]
        results.put(("ok", count))
    except Exception as error:                       # noqa: BLE001
        results.put(("error", f"{type(error).__name__}: {error}"))


def _child_init_db(db_path, results, non_idempotent=False):
    _child_setup(db_path)
    try:
        if non_idempotent:
            # 故意登记一个**非幂等**迁移：谁跑第二次就会 "table c0_once already exists"
            target = db_base.SCHEMA_VERSION + 1

            def _probe_migration(conn):
                conn.execute("CREATE TABLE c0_once (n INTEGER)")
                conn.execute("INSERT INTO c0_once (n) VALUES (1)")

            db_base._MIGRATIONS[target] = _probe_migration
            db_base.SCHEMA_VERSION = target
        db_base.init_db()
        results.put(("ok", db_base.SCHEMA_VERSION))
    except Exception as error:                       # noqa: BLE001
        results.put(("error", f"{type(error).__name__}: {error}"))


# ======================= 测试 =======================


@unittest.skipUnless(POSIX, "C0 写协调依赖 POSIX fcntl.flock")
class ConcurrencyTests(ElenvindTestCase):
    WORKERS = 4
    PER_WORKER = 25

    def setUp(self):
        super().setUp()
        _prepare_tables()

    # ---------- 辅助 ----------
    def _collect(self, results, count, timeout=30):
        outcomes = []
        for _ in range(count):
            try:
                outcomes.append(results.get(timeout=timeout))
            except queue.Empty:
                self.fail("子进程没有上报结果（很可能崩溃了）")
        errors = [payload for status, payload in outcomes if status == "error"]
        self.assertEqual(errors, [], f"子进程报错：{errors}")
        return outcomes

    def _join(self, processes, timeout=60):
        for process in processes:
            try:
                process.join(timeout=timeout)
                self.assertFalse(process.is_alive(),
                                 "子进程没有在期限内结束（疑似锁没有释放）")
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(timeout=10)

    def _query(self, sql, params=()):
        with connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _integrity(self):
        with connect() as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0]

    # ---------- 1. 多进程并发写入 ----------
    def test_concurrent_writers_lose_nothing(self):
        go = CTX.Event()
        results = CTX.Queue()
        processes = [
            CTX.Process(target=_child_insert_batch,
                        args=(self.db_path, index * self.PER_WORKER,
                              self.PER_WORKER, go, results))
            for index in range(self.WORKERS)
        ]
        for process in processes:
            process.start()
        go.set()                                  # 同时开闸：让它们真的去抢锁
        self._join(processes)
        self._collect(results, len(processes))

        expected = self.WORKERS * self.PER_WORKER
        tags = [row["tag"] for row in self._query("SELECT tag FROM c0_account")]
        self.assertEqual(len(tags), expected, "提交的行数不对（丢失或重复）")
        self.assertEqual(len(set(tags)), expected, "出现了重复行")
        self.assertEqual(self._integrity(), "ok")

    # ---------- 2. 临界区互斥（不是靠时间戳递增来证明） ----------
    def test_write_transactions_never_overlap(self):
        hold = 0.30
        attempting_a, entered_a = CTX.Event(), CTX.Event()
        attempting_b, entered_b = CTX.Event(), CTX.Event()
        results = CTX.Queue()
        first = CTX.Process(target=_child_probe_section,
                            args=(self.db_path, "A", hold, attempting_a, entered_a, results))
        second = CTX.Process(target=_child_probe_section,
                             args=(self.db_path, "B", hold, attempting_b, entered_b, results))

        first.start()
        self.assertTrue(entered_a.wait(20), "A 没有进入临界区")
        second.start()
        self.assertTrue(attempting_b.wait(20), "B 没有尝试进入临界区")
        self._join((first, second))
        self._collect(results, 2)

        rows = {row["owner"]: (row["entered_at"], row["left_at"])
                for row in self._query("SELECT owner, entered_at, left_at FROM c0_probe")}
        self.assertEqual(sorted(rows), ["A", "B"])
        for owner, (entered_at, left_at) in rows.items():
            with self.subTest(owner=owner):
                self.assertIsNotNone(left_at, f"{owner} 的临界区没有正常结束")
                self.assertGreaterEqual(left_at, entered_at)

        a_enter, a_leave = rows["A"]
        b_enter, b_leave = rows["B"]
        # 主要证据：两个临界区的时间区间**不重叠**
        self.assertFalse(min(a_leave, b_leave) > max(a_enter, b_enter),
                         f"两个写事务的临界区重叠了：A={rows['A']} B={rows['B']}")
        # 辅助证据：B 确实在 A 持锁期间尝试，并且真的等到了 A 释放
        self.assertGreaterEqual(b_enter, a_leave - 0.01,
                                f"B 在 A 释放锁之前就进入：A={rows['A']} B={rows['B']}")
        self.assertGreaterEqual(b_enter - a_enter, hold * 0.9,
                                "B 没有等待 A 的持锁时间，互斥可能失效")

    # ---------- 3. 进程崩溃恢复 ----------
    def test_crashed_writer_is_recovered(self):
        marker = "uncommitted-marker"
        receive, send = CTX.Pipe(duplex=False)
        child = CTX.Process(target=_child_crash_holder,
                            args=(self.db_path, send, marker, CRASH_BLOB_BYTES))
        child.start()
        try:
            self.assertTrue(receive.poll(30), "崩溃测试子进程没有进入事务")
            self.assertEqual(receive.recv(), "in-transaction")

            wal = Path(str(self.db_path) + "-wal")
            self.assertTrue(wal.exists(), "WAL 文件不存在")
            self.assertGreater(wal.stat().st_size, WAL_MIN_SIZE,
                               "未提交事务没有落到 WAL，崩溃测试会失去意义")

            os.kill(child.pid, signal.SIGKILL)      # 模拟进程被强杀
            child.join(timeout=15)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, -signal.SIGKILL)
        finally:
            send.close()
            if child.is_alive():                    # pragma: no cover - 兜底
                child.kill()
                child.join(timeout=10)

        # 父进程还活着：锁必须已由内核释放，新 writer 必须能恢复并完成
        results = CTX.Queue()
        recovery = CTX.Process(target=_child_write_marker,
                               args=(self.db_path, "after-crash", results))
        started = time.monotonic()
        recovery.start()
        self._join((recovery,), timeout=20)
        self._collect(results, 1)
        self.assertLess(time.monotonic() - started, 20,
                        "崩溃后的 writer 花的时间超过测试期限")

        self.assertEqual(
            self._query("SELECT COUNT(*) FROM c0_crash WHERE marker = ?",
                        (marker,))[0][0], 0,
            "未提交的数据在崩溃后出现了")
        self.assertEqual(self._query("SELECT COUNT(*) FROM c0_blob")[0][0], 0)
        self.assertEqual(self._integrity(), "ok")

    # ---------- 4. 迁移并发 ----------
    def test_concurrent_init_on_a_fresh_database(self):
        fresh = self.tmpdir / "fresh-concurrent.db"
        results = CTX.Queue()
        processes = [CTX.Process(target=_child_init_db, args=(fresh, results))
                     for _ in range(self.WORKERS)]
        for process in processes:
            process.start()
        self._join(processes)
        self._collect(results, len(processes))

        had = db_base.DB_PATH
        db_base.DB_PATH = fresh
        try:
            version = self._query("PRAGMA user_version")[0][0]
            tables = {row["name"] for row in
                      self._query("SELECT name FROM sqlite_master WHERE type='table'")}
            leftover = [name for name in tables if name.endswith("_legacy")]
            self.assertEqual(version, db_base.SCHEMA_VERSION)
            self.assertTrue({"user", "session", "comment", "login_attempts",
                             "register_attempts", "comment_rate"} <= tables)
            self.assertEqual(leftover, [])
            self.assertEqual(self._integrity(), "ok")
        finally:
            db_base.DB_PATH = had

    def test_concurrent_init_on_a_legacy_database_preserves_data(self):
        legacy = self.tmpdir / "legacy-concurrent.db"
        conn = sqlite3.connect(legacy)
        conn.executescript(LEGACY_SCHEMA_V1)
        conn.commit()
        conn.close()

        results = CTX.Queue()
        processes = [CTX.Process(target=_child_init_db, args=(legacy, results))
                     for _ in range(self.WORKERS)]
        for process in processes:
            process.start()
        self._join(processes)
        self._collect(results, len(processes))

        had = db_base.DB_PATH
        db_base.DB_PATH = legacy
        try:
            version = self._query("PRAGMA user_version")[0][0]
            comments = self._query("SELECT COUNT(*) FROM comment")[0][0]
            tables = {row["name"] for row in
                      self._query("SELECT name FROM sqlite_master WHERE type='table'")}
            comment_sql = self._query(
                "SELECT sql FROM sqlite_master WHERE name = 'comment'")[0][0] or ""
            self.assertEqual(version, db_base.SCHEMA_VERSION)
            self.assertEqual(comments, 2, "迁移丢了评论数据")
            self.assertEqual([name for name in tables if name.endswith("_legacy")], [])
            self.assertIn("ON DELETE SET NULL", " ".join(comment_sql.upper().split()))
            self.assertEqual(self._integrity(), "ok")
        finally:
            db_base.DB_PATH = had

    def test_non_idempotent_migration_runs_exactly_once(self):
        """并发启动时，非幂等迁移只能被执行一次（第二次会直接建表失败）。"""
        fresh = self.tmpdir / "non-idempotent.db"
        results = CTX.Queue()
        processes = [CTX.Process(target=_child_init_db,
                                 args=(fresh, results, True))
                     for _ in range(self.WORKERS)]
        for process in processes:
            process.start()
        self._join(processes)
        self._collect(results, len(processes))      # 任何一个进程重复执行都会报错

        had = db_base.DB_PATH
        db_base.DB_PATH = fresh
        try:
            rows = self._query("SELECT COUNT(*) FROM c0_once")[0][0]
            self.assertEqual(rows, 1, "非幂等迁移被重复执行了")
            self.assertEqual(self._query("PRAGMA user_version")[0][0],
                             db_base.SCHEMA_VERSION + 1)
        finally:
            db_base.DB_PATH = had

    # ---------- 5. 数据库路径 → 锁文件隔离 ----------
    def test_database_paths_have_independent_locks(self):
        other = self.tmpdir / "second.db"
        results = CTX.Queue()
        prepare = CTX.Process(target=_child_prepare_tables, args=(other, results))
        prepare.start()
        self._join((prepare,))
        self._collect(results, 1)

        self.assertNotEqual(lock_path_for(self.db_path), lock_path_for(other))

        # 父进程手工持有本库的写锁，然后验证另一个库的写进程**不被牵连**
        lock_path = lock_path_for(db_base.DB_PATH)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)

            other_results = CTX.Queue()
            other_writer = CTX.Process(target=_child_write_marker,
                                       args=(other, "other-db", other_results))
            started = time.monotonic()
            other_writer.start()
            self._join((other_writer,), timeout=20)
            self._collect(other_results, 1)
            self.assertLess(time.monotonic() - started, 20,
                            "持有 A 库写锁时，B 库的写进程被阻塞了（锁文件串了）")

            # 读路径同样不该被写锁阻塞
            read_results = CTX.Queue()
            reader = CTX.Process(target=_child_read_count, args=(self.db_path, read_results))
            reader.start()
            self._join((reader,), timeout=20)
            self._collect(read_results, 1)

            # 同一个库的写进程必须被这把锁挡住
            blocked_results = CTX.Queue()
            blocked = CTX.Process(target=_child_write_marker,
                                  args=(self.db_path, "blocked", blocked_results))
            blocked.start()
            try:
                time.sleep(0.5)
                self.assertTrue(blocked.is_alive(),
                                "同一数据库的写进程没有被写锁挡住（互斥失效）")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
            self._join((blocked,), timeout=20)
            self._collect(blocked_results, 1)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
