"""并发边界：限流判定、唯一约束与会话轮换必须在**真正并发**下成立。

为什么需要这些用例：顺序调用无法证明"判定与写入在同一个事务里" —— 例如评论
限流若在事务外判断，顺序调用看起来也是对的，只有并发才会超发。这里用线程 +
WSGI 直调（`tests/support.py`）真正并发发起请求，不启动 Gunicorn。
"""
from __future__ import annotations

import threading
import unittest

from tests import support
from tests.support import ARTICLE_SLUG


def run_concurrently(function, count: int):
    """并发跑 `count` 次 `function(index)`，返回结果列表（异常原样返回）。"""
    results: list = [None] * count

    def worker(index: int):
        try:
            results[index] = function(index)
        except Exception as exc:                  # 线程内异常必须暴露，不能吞掉
            results[index] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def errors_of(results) -> list:
    return [result for result in results if isinstance(result, Exception)]


class CommentRateLimit(unittest.TestCase):
    """并发评论不得超发：判定与插入必须在同一个写事务内。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        config = support.live_config()
        self._saved = config["comment_limits"]
        config["comment_limits"] = {"max_per_user": 1, "max_per_ip": 100,
                                    "window_seconds": 60}
        self.user = support.Session()
        self.user.create_account("Conc", "conc@example.test", "correct-horse-1")

    def tearDown(self):
        support.live_config()["comment_limits"] = self._saved

    def test_no_overshoot_under_concurrency(self):
        def post(index):
            client = support.Session()
            client.cookies = dict(self.user.cookies)
            return client.comment(ARTICLE_SLUG, f"concurrent-{index}").status

        results = run_concurrently(post, 6)
        self.assertEqual(errors_of(results), [], f"threads raised: {errors_of(results)}")
        self.assertEqual(results.count(302), 1, f"exactly one may pass: {results}")
        self.assertEqual(results.count(429), 5, f"the rest must be rate limited: {results}")
        self.assertEqual(support.query("SELECT COUNT(*) FROM comment")[0][0], 1)


class RegistrationUniqueness(unittest.TestCase):
    """并发注册同一邮箱只能成功一次（UNIQUE 约束是最终权威）。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()

    def test_same_email_cannot_be_registered_twice(self):
        def register(index):
            return support.Session().register("Dup", "dup@example.test",
                                              "correct-horse-1").status

        results = run_concurrently(register, 6)
        self.assertEqual(errors_of(results), [], f"threads raised: {errors_of(results)}")
        self.assertEqual(results.count(302), 1, f"exactly one may pass: {results}")
        self.assertEqual(
            support.query("SELECT COUNT(*) FROM user WHERE email = 'dup@example.test'")[0][0], 1)


class SessionRotation(unittest.TestCase):
    """并发登录同一账号只能留下一个会话（轮换必须是一个事务）。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        config = support.live_config()
        self._saved = config["login_limits"]
        config["login_limits"] = {"max_email_failures": 100, "email_window_seconds": 86400,
                                  "max_ip_failures": 500, "ip_window_seconds": 900,
                                  "max_global_failures": 5000, "global_window_seconds": 900}
        support.Session().create_account("Rot", "rot@example.test", "correct-horse-1")

    def tearDown(self):
        support.live_config()["login_limits"] = self._saved

    def test_concurrent_logins_leave_a_single_session(self):
        def login(index):
            client = support.Session()
            client.login("rot@example.test", "correct-horse-1")
            return client.cookies.get("session")

        results = run_concurrently(login, 4)
        self.assertEqual(errors_of(results), [], f"threads raised: {errors_of(results)}")
        self.assertTrue([token for token in results if token], results)
        rows = support.query("SELECT COUNT(*) FROM session")[0][0]
        self.assertEqual(rows, 1, f"rotation must be atomic: found {rows} session(s)")


if __name__ == "__main__":
    unittest.main()
