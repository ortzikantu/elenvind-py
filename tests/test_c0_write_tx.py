"""`write_tx()` 单元测试：事务语义、异常路径、资源释放、锁语义。

这是 C0 的**最小可信面**：唯一写事务入口本身的行为。多进程竞争、临界区互斥、
崩溃恢复、迁移并发在 `tests/test_c0_concurrency.py` 里用真实进程覆盖。

覆盖：
1. 正常退出 → 提交；异常 → 回滚并原样重抛；
2. `BaseException`（KeyboardInterrupt）也回滚，不留半个事务；
3. 连接一定关闭、锁一定释放（后续写入可立即成功）；
4. 提交失败**不得**被当成成功（数据不在、异常抛出、锁释放）；
5. 嵌套调用被拒绝（否则会在 flock 上自死锁）；
6. 锁文件独立于数据库、路径随 DB_PATH 动态派生、且不被删除；
7. 连接级 PRAGMA 保留（foreign_keys / busy_timeout / WAL 由初始化确立）；
8. 读路径（`connect()`）不参与 flock：别人持锁时读取照常成功。
"""
import errno
import os
import sqlite3
import unittest
from pathlib import Path

from tests.support import ElenvindTestCase

from elenvind import db
from elenvind.db import connection as db_connection
from elenvind.db import transaction as db_transaction
from elenvind.db import (
    connect,
    get_connection,
    init_db,
    lock_path_for,
    write_tx,
)

try:                                    # fcntl 只在 POSIX 上存在
    import fcntl
except ImportError:                     # pragma: no cover - 非 POSIX 平台
    fcntl = None

#: C0 依赖 POSIX flock；其它平台上这些测试没有意义（实现会显式报错）
POSIX = hasattr(os, "fork") and fcntl is not None


@unittest.skipUnless(POSIX, "C0 写协调依赖 POSIX fcntl.flock")
class WriteTransactionTests(ElenvindTestCase):
    """write_tx() 的事务与资源语义。"""

    def setUp(self):
        super().setUp()
        with write_tx() as conn:
            conn.execute("CREATE TABLE c0_write (id INTEGER PRIMARY KEY, tag TEXT UNIQUE)")

    def _tags(self):
        with connect() as conn:
            return sorted(row["tag"] for row in
                          conn.execute("SELECT tag FROM c0_write"))

    def test_normal_exit_commits(self):
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('committed')")
        self.assertEqual(self._tags(), ["committed"])

    def test_exception_rolls_back_and_propagates(self):
        class BusinessError(RuntimeError):
            pass

        with self.assertRaises(BusinessError):
            with write_tx() as conn:
                conn.execute("INSERT INTO c0_write (tag) VALUES ('must-not-exist')")
                raise BusinessError("business failure")
        self.assertEqual(self._tags(), [])

    def test_base_exception_also_rolls_back(self):
        """KeyboardInterrupt 之类的 BaseException 同样不能留下未提交数据。"""
        with self.assertRaises(KeyboardInterrupt):
            with write_tx() as conn:
                conn.execute("INSERT INTO c0_write (tag) VALUES ('interrupted')")
                raise KeyboardInterrupt()
        self.assertEqual(self._tags(), [])

    def test_explicit_rollback_in_body_is_honoured(self):
        """"判定后放弃"的写法（限流拒绝路径）必须不落任何数据。"""
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('discarded')")
            conn.rollback()
        self.assertEqual(self._tags(), [])

    def test_connection_is_closed_and_lock_released(self):
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('first')")
            held = conn
        with self.assertRaises(sqlite3.ProgrammingError):
            held.execute("SELECT 1")           # 连接已关闭

        # 锁已释放：同一线程立刻可以再开一个写事务（否则这里会自死锁）
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('second')")
        self.assertEqual(self._tags(), ["first", "second"])

    def test_nested_write_tx_is_rejected(self):
        with self.assertRaises(RuntimeError) as caught:
            with write_tx():
                with write_tx():
                    pass
        self.assertIn("不可嵌套", str(caught.exception))

        # 守卫必须在异常后复位，否则后续写入会被永久拒绝
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('after-nesting')")
        self.assertEqual(self._tags(), ["after-nesting"])

    def test_commit_failure_is_not_reported_as_success(self):
        """COMMIT 失败必须抛异常、回滚、释放锁 —— 绝不假装成功。"""
        real_get_connection = db.get_connection

        class CommitAlwaysFails:
            def __init__(self, inner):
                self._inner = inner

            def commit(self):
                raise sqlite3.OperationalError("simulated commit failure")

            def __getattr__(self, name):
                return getattr(self._inner, name)

        db_transaction.get_connection = lambda foreign_keys=True: CommitAlwaysFails(
            real_get_connection(foreign_keys=foreign_keys))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with write_tx() as conn:
                    conn.execute("INSERT INTO c0_write (tag) VALUES ('phantom')")
        finally:
            db_transaction.get_connection = real_get_connection

        self.assertEqual(self._tags(), [])      # 没有"看不见的提交"
        with write_tx() as conn:                # 锁已释放
            conn.execute("INSERT INTO c0_write (tag) VALUES ('after-failure')")
        self.assertEqual(self._tags(), ["after-failure"])

    def test_connection_settings_are_preserved(self):
        with write_tx() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertGreaterEqual(
                conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_lock_is_held_for_the_whole_transaction(self):
        """跨进程锁必须覆盖**整个**事务窗口，而不是只锁住 BEGIN 那一瞬间。

        做法：事务进行中（业务 SQL 已经执行、还没提交），用**另一个 fd**
        对同一个锁文件做非阻塞 `LOCK_EX` —— 必须被拒绝（EWOULDBLOCK）。
        flock 的锁是"每个打开文件描述"独立的，因此同一进程内换一个 fd 也会
        冲突，这个判定是可靠的。

        这一条是**针对 flock 层**的证据：如果把 flock 去掉（变异测试），
        非阻塞取锁会成功，测试立刻失败。
        """
        path = lock_path_for(db_connection.DB_PATH)
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('scope')")
            probe = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with self.assertRaises(OSError) as caught:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn(caught.exception.errno,
                              (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK),
                              "事务进行中却没锁住锁文件")
            finally:
                os.close(probe)

        # 事务结束后必须立刻可以拿到锁（否则就是漏掉了 unlock）
        probe = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        finally:
            os.close(probe)

    def test_lock_is_held_on_the_rollback_path_too(self):
        """异常路径同样必须持锁到事务收尾（回滚+解锁在 finally 里）。"""
        path = lock_path_for(db_connection.DB_PATH)
        with self.assertRaises(ValueError):
            with write_tx() as conn:
                conn.execute("INSERT INTO c0_write (tag) VALUES ('rollback-scope')")
                probe = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    with self.assertRaises(OSError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                raise ValueError("boom")
        self.assertEqual(self._tags(), [])

    def test_begin_failure_releases_connection_and_lock(self):
        """事务**开始**就失败时，连接与锁同样必须被释放（不能泄漏 fd）。

        模拟方式：让一条"协议外"的连接先占住 SQLite 写锁，再把 busy_timeout
        调到极小，于是 write_tx 的 `BEGIN IMMEDIATE` 会立刻报
        "database is locked"。要求：异常抛出、锁释放、fd 不泄漏。
        """
        holder = sqlite3.connect(str(db_connection.DB_PATH), timeout=1)
        holder.isolation_level = None
        holder.execute("BEGIN IMMEDIATE")        # 非 write_tx 的连接占住写锁
        original_timeout = db_connection._BUSY_TIMEOUT_MS
        db_connection._BUSY_TIMEOUT_MS = 50            # 别让测试等默认的 5 秒
        fds_before = len(os.listdir("/proc/self/fd"))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with write_tx() as conn:
                    conn.execute("INSERT INTO c0_write (tag) VALUES ('never')")
        finally:
            db_connection._BUSY_TIMEOUT_MS = original_timeout
            holder.rollback()
            holder.close()
        fds_after = len(os.listdir("/proc/self/fd"))
        self.assertLessEqual(fds_after, fds_before,
                             "事务开始失败时泄漏了文件描述符（连接没关）")

        # 锁必须已释放：立刻可以正常写入
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('after-begin-failure')")
        self.assertEqual(self._tags(), ["after-begin-failure"])

    def test_sqlite_write_lock_is_taken_inside_write_tx(self):
        """`BEGIN IMMEDIATE` 的语义必须保留：事务里 SQLite 写锁已经拿到。

        做法：在 write_tx 内部用**另一条连接**（timeout 极小）尝试
        `BEGIN IMMEDIATE` —— 若我们只是 DEFERRED 事务，它会成功；
        我们要求它失败（"database is locked"）。
        """
        with write_tx() as conn:
            conn.execute("INSERT INTO c0_write (tag) VALUES ('sqlite-lock')")
            other = sqlite3.connect(str(db_connection.DB_PATH), timeout=0.05)
            other.isolation_level = None
            try:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    other.execute("BEGIN IMMEDIATE")
                self.assertIn("locked", str(caught.exception).lower())
            finally:
                other.close()

    def test_foreign_keys_can_be_disabled_for_migrations(self):
        with write_tx(foreign_keys=False) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        with write_tx() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)


@unittest.skipUnless(POSIX, "C0 写协调依赖 POSIX fcntl.flock")
class LockFileTests(ElenvindTestCase):
    """锁文件：独立、动态派生、稳定存在。"""

    def test_lock_path_is_derived_from_the_database_path(self):
        self.assertEqual(lock_path_for(self.db_path),
                         self.db_path.with_name(self.db_path.name + ".write.lock"))
        self.assertNotEqual(lock_path_for(self.db_path), Path(self.db_path))

    def test_lock_path_follows_db_path_changes(self):
        first = lock_path_for(self.db_path)
        other = self.db_path.with_name("other.db")
        self.assertNotEqual(lock_path_for(other), first)
        self.assertEqual(lock_path_for(other).name, "other.db.write.lock")

    def test_lock_file_is_independent_and_stable(self):
        with write_tx() as conn:
            conn.execute("CREATE TABLE c0_lock (n INTEGER)")
        # 注意：必须通过模块属性取当前数据库路径（测试夹具会替换 DB_PATH），
        # 直接 `from ... import DB_PATH` 拿到的是导入时的快照 —— 正是实现里
        # 刻意避免的那种"永久缓存路径"错误。
        path = lock_path_for(db_connection.DB_PATH)
        self.assertTrue(path.exists())
        # 数据库本体绝不能是锁文件
        self.assertNotEqual(path, Path(db_connection.DB_PATH))
        inode_before = os.stat(path).st_ino
        for _ in range(3):
            with write_tx() as conn:
                conn.execute("INSERT INTO c0_lock (n) VALUES (1)")
        self.assertTrue(path.exists(), "锁文件不该在释放后被删除")
        self.assertEqual(os.stat(path).st_ino, inode_before,
                         "锁文件 inode 变了：不同进程可能锁住不同文件")

    def test_reads_are_not_blocked_by_a_held_write_lock(self):
        """读路径不参与 flock：别人持写锁时 SELECT 仍然可用（这是设计目标）。"""
        path = lock_path_for(db_connection.DB_PATH)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with connect() as conn:                 # 不加锁的读
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 0,
                    True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_wal_is_established_by_initialization(self):
        """WAL 由 init_db()（在 write_tx 内）确立，普通连接不再逐个切换。"""
        init_db()
        for _ in range(2):
            conn = get_connection()
            try:
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
