"""评论系统补充测试：跨文章回复、成环数据、并发上限，以及树构建复杂度基准。

复杂度基准对应规格第十五节：100 / 500 / 1000 / 5000 条评论的树构建，
必须接近线性（不是 O(n²)），且任何深度都不能触发 Python 递归上限。

树展开现在由 `modules.blog.logic.build_comment_rows` 的同一套算法负责
（children_by_parent + 显式栈）；本模块的性能基准对内存数据复刻该算法，
因此仍然能守住"线性 + 无递归"这条契约。
"""
import time
import unittest

from tests.support import ElenvindTestCase

from elenvind.core.db_base import connect
from elenvind.core.db_comment import create_comment, get_comment_by_id, get_comments_by_article
from elenvind.modules.blog import logic as blog


def expand_tree(comments):
    """树展开 —— 直接调生产实现 `core.db_comment.flatten_comment_tree`。

    返回 `(ordered, by_id)` 以保持原调用点不变。
    这里曾经有一份"与生产实现同构"的副本；副本与生产**都**缺少对
    不可达环的处理（root + 环 2<->3 时两边都只渲染出 root），
    而副本多出的 `seen` 又让缺陷看起来已经被处理。详见 test_comments.py
    里 CommentTreeTests 的说明。
    """
    from elenvind.core.db_comment import flatten_comment_tree

    by_id = {row["id"]: row for row in comments}
    return flatten_comment_tree(comments), by_id


class CrossArticleReplyTests(ElenvindTestCase):
    def setUp(self):
        super().setUp()
        self.write_article("post-a", "body")
        self.write_article("post-b", "body")
        self.create_user(nickname="Ann", email="ann@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com", "correct horse battery")
        self.cookies = self.app_cookies(session=self.session, csrf=self.csrf)

    def test_reply_to_comment_of_another_article_is_rejected(self):
        other = create_comment("post-b", 1, "other article comment")
        response = self.app.request("POST", "/article/post-a/comment",
                                    form={"csrf_token": self.csrf, "content": "cross",
                                          "reply_to": str(other)},
                                    cookies=self.cookies)
        # 回复目标非法 = 表单输入问题 -> 400，且不写入任何评论
        self.assertEqual(response.status, 400)
        self.assertEqual(len(get_comments_by_article("post-a")), 0)

    def test_delete_comment_of_another_article_is_rejected(self):
        other = create_comment("post-b", 1, "other article comment")
        response = self.app.request("POST", f"/article/post-a/comment/delete/{other}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertEqual(get_comment_by_id(other)["is_deleted"], 0)

    def test_reply_to_root_of_same_article_is_accepted(self):
        root = create_comment("post-a", 1, "root")
        response = self.app.request("POST", "/article/post-a/comment",
                                    form={"csrf_token": self.csrf, "content": "reply",
                                          "reply_to": str(root)},
                                    cookies=self.cookies)
        self.assertEqual(response.status, 302)
        comments = get_comments_by_article("post-a")
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[-1]["parent_id"], root)

    def test_comment_on_empty_slug_is_404(self):
        response = self.app.request("POST", "/article//comment",
                                    form={"csrf_token": self.csrf, "content": "x"},
                                    cookies=self.cookies)
        self.assertEqual(response.status, 404)

    def test_comment_on_deleted_article_is_404(self):
        path = self.articles_dir / "post-a.md"
        path.unlink()
        response = self.app.request("POST", "/article/post-a/comment",
                                    form={"csrf_token": self.csrf, "content": "x"},
                                    cookies=self.cookies)
        self.assertEqual(response.status, 404)
        self.assertEqual(len(get_comments_by_article("post-a")), 0)


class CorruptCommentDataTests(ElenvindTestCase):
    """数据库被外部改坏时（成环 / 悬空父），页面与写入路径都必须有确定行为。"""

    def setUp(self):
        super().setUp()
        self.write_article("post", "body")
        self.create_user(email="corrupt@example.com")
        self.session, self.csrf = self.login_ok("corrupt@example.com",
                                                "correct horse battery")
        self.cookies = self.app_cookies(session=self.session, csrf=self.csrf)

    def test_surface_level_cycle_does_not_hang_rendering(self):
        first = create_comment("post", 1, "one")
        second = create_comment("post", 1, "two", parent_id=first)
        with connect() as conn:
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?", (second, first))
            conn.commit()
        response = self.app.request("GET", "/article/post")
        self.assertEqual(response.status, 200)

    def test_reply_depth_is_bounded_when_chain_is_corrupt(self):
        """父链被人为拉长/成环时，层级计算必须是常数级有界的，不能死循环。"""
        from elenvind.modules.blog import logic as _blog_logic
        from elenvind.core.db_comment import get_comment_by_id

        self._config["max_comment_depth"] = 3
        ids = [create_comment("post", 1, f"c{index}") for index in range(6)]
        with connect() as conn:
            for previous, current in zip(ids, ids[1:]):
                conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                             (previous, current))
            # 制造环：第一个指向最后一个
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?", (ids[-1], ids[0]))
            conn.commit()

        start = time.perf_counter()
        depth = _blog_logic.comment_depth(get_comment_by_id(ids[-1]))
        elapsed = time.perf_counter() - start
        self.assertLessEqual(depth, self._config["max_comment_depth"] + 1)
        self.assertLess(elapsed, 1.0)

    def test_dangling_parent_is_treated_as_root_when_rendering(self):
        lone = create_comment("post", 1, "lonely")
        with connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("UPDATE comment SET parent_id = 999999 WHERE id = ?", (lone,))
            conn.commit()
        response = self.app.request("GET", "/article/post")
        self.assertEqual(response.status, 200)
        self.assertIn("lonely", response.text)


class CommentCapConcurrencyTests(ElenvindTestCase):
    """单篇文章评论上限必须在写事务内判定（并发下不能超发）。"""

    def test_cap_is_enforced_inside_the_write_transaction(self):
        from elenvind.core.db_comment_rate import try_post_comment

        user_id, _ = self.create_user()
        for index in range(3):
            outcome = try_post_comment("slug", user_id, "1.2.3.4", f"c{index}", None,
                                       max_per_user=100, max_per_ip=100, window_seconds=60,
                                       max_per_article=3, created_at="2026-01-01T00:00:00")
            self.assertEqual(outcome, "ok")
        blocked = try_post_comment("slug", user_id, "1.2.3.4", "overflow", None,
                                   max_per_user=100, max_per_ip=100, window_seconds=60,
                                   max_per_article=3, created_at="2026-01-01T00:00:01")
        self.assertEqual(blocked, "too_many")
        with connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM comment WHERE article_slug = 'slug'"
                                 ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_interleaved_writers_cannot_exceed_cap(self):
        """多连接交替写入（模拟并发）时，上限依然成立。"""
        from elenvind.core.db_comment_rate import try_post_comment

        user_id, _ = self.create_user()
        writers = [f"10.0.0.{index}" for index in range(3)]
        accepted = 0
        for round_index in range(4):
            for writer in writers:
                outcome = try_post_comment(
                    "shared", user_id, writer, f"c{round_index}-{writer}", None,
                    max_per_user=100, max_per_ip=100, window_seconds=60,
                    max_per_article=5, created_at="2026-01-01T00:00:00")
                if outcome == "ok":
                    accepted += 1
        self.assertEqual(accepted, 5)
        with connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM comment WHERE article_slug = 'shared'"
                                 ).fetchone()[0]
        self.assertEqual(count, 5)


class CommentTreePerformanceTests(unittest.TestCase):
    """树构建复杂度基准（规格第十五节）。"""

    @staticmethod
    def _row(cid, parent_id):
        return {"id": cid, "parent_id": parent_id, "created_at": "", "user_id": 1,
                "article_slug": "s", "content": "", "is_deleted": 0,
                "nickname": "n", "user_deleted": 0}

    def _chain(self, count):
        rows = [self._row(1, None)]
        rows += [self._row(index, index - 1) for index in range(2, count + 1)]
        return rows

    def _fanout(self, count):
        return [self._row(1, None)] + [self._row(index, 1) for index in range(2, count + 1)]

    def test_required_sizes_complete_and_scale_linearly(self):
        timings = {}
        for size in (100, 500, 1000, 5000):
            with self.subTest(size=size):
                rows = self._fanout(size)
                start = time.perf_counter()
                ordered, _ = expand_tree(rows)
                elapsed = time.perf_counter() - start
                timings[size] = elapsed
                self.assertEqual(len(ordered), size)
                self.assertLess(elapsed, 1.0, f"{size} comments took {elapsed:.5f}s")

        # 线性判断：最大规模耗时不应超过最小规模的 size 比值（留 8 倍余量）
        ratio = timings[5000] / max(timings[100], 1e-6)
        self.assertLess(ratio, (5000 / 100) * 8,
                        f"build_tree looks super-linear: {timings}")

    def test_deep_chain_does_not_recurse(self):
        for size in (100, 500, 1000, 5000):
            with self.subTest(size=size):
                ordered, _ = expand_tree(self._chain(size))
                self.assertEqual(len(ordered), size)
                self.assertEqual(ordered[-1][1], size)

    def test_mixed_shape_is_linear(self):
        rows = []
        cid = 0
        for root_index in range(100):
            cid += 1
            root = cid
            rows.append(self._row(root, None))
            for _ in range(49):
                cid += 1
                rows.append(self._row(cid, root))
        start = time.perf_counter()
        ordered, _ = expand_tree(rows)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(ordered), 5000)
        self.assertLess(elapsed, 1.0, f"mixed tree took {elapsed:.5f}s")


if __name__ == "__main__":
    unittest.main()
