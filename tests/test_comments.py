"""评论系统测试：树组装（O(n)、无递归）、孤儿评论、层级上限、限流、增删恢复权限。"""
import time
import unittest

from tests.support import ElenvindTestCase

from elenvind.db_base import get_connection
from elenvind.db_comment import (
    create_comment,
    get_comment_by_id,
    get_comments_by_article,
)
from elenvind.db_comment_rate import try_post_comment
from elenvind.view_partials_comment import build_tree, render_comments


class CommentTreeTests(unittest.TestCase):
    def _row(self, cid, parent_id):
        return {"id": cid, "parent_id": parent_id, "created_at": "", "user_id": 1,
                "article_slug": "s", "content": "", "is_deleted": 0,
                "nickname": "n", "user_deleted": 0}

    def test_flat_tree(self):
        comments = [self._row(1, None), self._row(2, None)]
        ordered, by_id = build_tree(comments)
        self.assertEqual([(c["id"], d) for c, d in ordered], [(1, 1), (2, 1)])
        self.assertEqual(set(by_id), {1, 2})

    def test_nested_tree_depth_first_order(self):
        comments = [self._row(1, None), self._row(2, 1), self._row(3, 2),
                    self._row(4, 1), self._row(5, None), self._row(6, 5)]
        ordered, _ = build_tree(comments)
        self.assertEqual([c["id"] for c, _ in ordered], [1, 2, 3, 4, 5, 6])
        self.assertEqual([d for _, d in ordered], [1, 2, 3, 2, 1, 2])

    def test_orphan_comments_become_roots(self):
        """父评论不存在（被物理清除）的子评论必须仍能显示为顶层。"""
        comments = [self._row(10, 999), self._row(11, 10)]
        ordered, _ = build_tree(comments)
        self.assertEqual([(c["id"], d) for c, d in ordered], [(10, 1), (11, 2)])

    def test_cycle_does_not_hang_or_recurse(self):
        """损坏数据成环时不能死循环，也不能让页面崩掉。"""
        broken = [{"id": 1, "parent_id": 2}, {"id": 2, "parent_id": 1}]
        for row in broken:
            row.update({"created_at": "", "user_id": 1, "article_slug": "s",
                        "content": "", "is_deleted": 0, "nickname": "n",
                        "user_deleted": 0})
        # 两个节点互为父：都不是根，因此结果为空（而不是无限循环）
        ordered, _ = build_tree(broken)
        self.assertEqual(ordered, [])

    def test_deep_tree_is_iterative_and_linear(self):
        """10,000 条评论的深链不能在递归或 O(n²) 上爆炸。"""
        count = 10_000
        comments = [self._row(1, None)]
        comments += [self._row(i, i - 1) for i in range(2, count + 1)]

        start = time.perf_counter()
        ordered, _ = build_tree(comments)
        elapsed = time.perf_counter() - start

        self.assertEqual(len(ordered), count)
        self.assertEqual(ordered[-1][1], count)     # 深度 = count
        self.assertLess(elapsed, 2.0, f"build_tree too slow: {elapsed:.3f}s")

    def test_wide_tree_is_linear(self):
        count = 10_000
        comments = [self._row(1, None)] + [self._row(i, 1) for i in range(2, count + 1)]
        start = time.perf_counter()
        ordered, _ = build_tree(comments)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(ordered), count)
        self.assertLess(elapsed, 2.0, f"build_tree too slow: {elapsed:.3f}s")


class CommentRenderTests(ElenvindTestCase):
    def test_empty_comment_section(self):
        self.write_article("post", "body")
        html = render_comments("post", None)
        self.assertIn("comments", html)
        self.assertIn("Sign in", html) if "Sign in" in html else None
        self.assertIn("id='comments'", html)

    def test_comment_content_is_escaped(self):
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        create_comment("post", user_id, "<script>alert(1)</script>\nsecond line")
        html = render_comments("post", None)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<br>", html)   # 换行保留

    def test_deleted_comment_is_masked_for_visitors(self):
        from elenvind.db_comment import soft_delete_comment
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        comment_id = create_comment("post", user_id, "secret message")
        soft_delete_comment(comment_id)
        html = render_comments("post", None)
        self.assertNotIn("secret message", html)
        self.assertIn("█", html)

    def test_deleted_comment_visible_to_admin_with_strikethrough(self):
        from elenvind.db_comment import soft_delete_comment
        admin_id, admin_pw = self.create_user(nickname="Boss", email="boss@example.com")
        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.write_article("post", "body")
        comment_id = create_comment("post", user_id, "flagged text")
        soft_delete_comment(comment_id)
        admin_row = {"id": admin_id, "nickname": "Boss"}
        html = render_comments("post", admin_row, csrf_token="c" * 43)
        self.assertIn("flagged text", html)
        self.assertIn("is-deleted", html)
        self.assertIn("Restore", html)

    def test_reply_hint_uses_parent_author(self):
        author_id, _ = self.create_user(nickname="Parent & Co", email="p@example.com")
        reply_id, _ = self.create_user(nickname="Replier", email="r@example.com")
        self.write_article("post", "body")
        parent = create_comment("post", author_id, "parent text")
        create_comment("post", reply_id, "reply text", parent_id=parent)
        viewer = {"id": reply_id, "nickname": "Replier"}
        html = render_comments("post", viewer, reply_to=parent, csrf_token="c" * 43)
        self.assertIn("Replying to", html)
        self.assertIn("Parent &amp; Co", html)
        self.assertNotIn("&amp;amp;", html)   # 不得双重转义

    def test_depth_limit_hides_reply_link(self):
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        self._config["max_comment_depth"] = 2
        first = create_comment("post", user_id, "level 1")
        second = create_comment("post", user_id, "level 2", parent_id=first)
        html = render_comments("post", {"id": user_id, "nickname": "A"},
                               csrf_token="c" * 43)
        # 深度 2 的评论不再有回复入口
        self.assertNotIn(f'reply_to={second}', html)
        self.assertIn(f'reply_to={first}', html)


class CommentHttpTests(ElenvindTestCase):
    def setUp(self):
        super().setUp()
        self.write_article("post", "body")
        self.user_id, self.password = self.create_user(nickname="Ann", email="ann@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com", self.password)
        self.cookies = self.app_cookies(session=self.session, csrf=self.csrf)

    def _post_comment(self, content="hello", reply_to=None, slug="post", cookies=None):
        form = {"csrf_token": self.csrf, "content": content}
        if reply_to is not None:
            form["reply_to"] = str(reply_to)
        return self.app.request("POST", f"/article/{slug}/comment", form=form,
                                cookies=cookies or self.cookies)

    def test_create_root_comment(self):
        response = self._post_comment("first comment")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/article/post#comments")
        comments = get_comments_by_article("post")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["content"], "first comment")

    def test_create_reply(self):
        self._post_comment("root")
        root_id = get_comments_by_article("post")[0]["id"]
        response = self._post_comment("reply", reply_to=root_id)
        self.assertEqual(response.status, 302)
        comments = get_comments_by_article("post")
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[1]["parent_id"], root_id)

    def test_reply_to_missing_comment_is_rejected(self):
        response = self._post_comment("reply", reply_to=99999)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(get_comments_by_article("post")), 0)

    def test_reply_to_deleted_comment_is_rejected(self):
        from elenvind.db_comment import soft_delete_comment
        self._post_comment("root")
        root_id = get_comments_by_article("post")[0]["id"]
        soft_delete_comment(root_id)
        response = self._post_comment("reply", reply_to=root_id)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(get_comments_by_article("post")), 1)

    def test_reply_depth_limit_is_enforced(self):
        self._config["max_comment_depth"] = 3
        parent = None
        for level in range(1, 4):
            response = self._post_comment(f"level {level}", reply_to=parent)
            self.assertEqual(response.status, 302, response.text[:200])
            parent = get_comments_by_article("post")[-1]["id"]
        response = self._post_comment("too deep", reply_to=parent)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(get_comments_by_article("post")), 3)

    def test_comment_on_unknown_article_is_404(self):
        response = self._post_comment("hi", slug="nope")
        self.assertEqual(response.status, 404)

    def test_comment_requires_login(self):
        response = self.app.request("POST", "/article/post/comment",
                                    form={"csrf_token": self.csrf, "content": "hi"},
                                    cookies={self.csrf_cookie_name(): self.csrf})
        self.assertEqual(response.status, 403)

    def test_empty_and_oversized_content_rejected(self):
        self.assertEqual(self._post_comment("   ").status, 400)
        self._config["max_length"] = 10
        response = self._post_comment("x" * 11)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(get_comments_by_article("post")), 0)

    def test_rate_limit_per_user(self):
        self._config["comment_limits"] = {"max_per_user": 2, "max_per_ip": 100,
                                          "window_seconds": 60}
        self._post_comment("1")
        self._post_comment("2")
        response = self._post_comment("3")
        self.assertEqual(response.status, 429)
        self.assertEqual(len(get_comments_by_article("post")), 2)

    def test_rate_limit_per_ip(self):
        self._config["comment_limits"] = {"max_per_user": 100, "max_per_ip": 2,
                                          "window_seconds": 60}
        self._post_comment("1")
        self._post_comment("2")
        response = self._post_comment("3")
        self.assertEqual(response.status, 429)

    def test_rate_limit_hit_does_not_write_rate_rows(self):
        """限流命中时不应把自己也记进流水（否则流量会放大成表增长）。"""
        self._config["comment_limits"] = {"max_per_user": 1, "max_per_ip": 100,
                                          "window_seconds": 60}
        self._post_comment("1")
        self._post_comment("2")
        self._post_comment("3")
        with get_connection() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM comment_rate").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_max_comments_per_article(self):
        self._config["max_comments_per_article"] = 2
        self._post_comment("1")
        self._post_comment("2")
        response = self._post_comment("3")
        self.assertEqual(response.status, 429)

    def test_author_can_delete_own_comment(self):
        self._post_comment("mine")
        comment_id = get_comments_by_article("post")[0]["id"]
        response = self.app.request("POST", f"/article/post/comment/delete/{comment_id}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 302)
        self.assertEqual(get_comment_by_id(comment_id)["is_deleted"], 1)

    def test_author_cannot_delete_others_comment(self):
        # 把管理员让给一个不存在的 id，使当前用户（id=1）成为普通用户
        self._config["admin_user_id"] = 999
        other_id, _ = self.create_user(nickname="Bob", email="bob@example.com")
        comment_id = create_comment("post", other_id, "not yours")
        response = self.app.request("POST", f"/article/post/comment/delete/{comment_id}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertEqual(get_comment_by_id(comment_id)["is_deleted"], 0)

    def test_author_cannot_restore_comment(self):
        from elenvind.db_comment import soft_delete_comment
        self._config["admin_user_id"] = 999
        self._post_comment("mine")
        comment_id = get_comments_by_article("post")[0]["id"]
        soft_delete_comment(comment_id)
        response = self.app.request("POST", f"/article/post/comment/restore/{comment_id}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 403)

    def test_admin_can_delete_and_restore_any_comment(self):
        other_id, _ = self.create_user(nickname="Ann2", email="ann2@example.com")
        comment_id = create_comment("post", other_id, "spam")
        # 默认 admin_user_id = 1，当前登录用户 id 为 1（首个注册用户）
        admin_session, admin_csrf = self.login_ok("ann@example.com", self.password)
        admin_cookies = self.app_cookies(session=admin_session, csrf=admin_csrf)

        delete = self.app.request("POST", f"/article/post/comment/delete/{comment_id}",
                                  form={"csrf_token": admin_csrf}, cookies=admin_cookies)
        self.assertEqual(delete.status, 302)
        self.assertEqual(get_comment_by_id(comment_id)["is_deleted"], 1)

        restore = self.app.request("POST", f"/article/post/comment/restore/{comment_id}",
                                   form={"csrf_token": admin_csrf}, cookies=admin_cookies)
        self.assertEqual(restore.status, 302)
        self.assertEqual(get_comment_by_id(comment_id)["is_deleted"], 0)

    def test_non_admin_id_1_is_not_admin(self):
        """admin_user_id 可配置：默认 id=1 以外的用户没有管理员权限。"""
        self._config["admin_user_id"] = 999
        other_id, _ = self.create_user(nickname="NotBoss", email="notboss@example.com")
        comment_id = create_comment("post", other_id, "spam")
        response = self.app.request("POST", f"/article/post/comment/restore/{comment_id}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 403)

    def test_missing_admin_config_means_no_admin(self):
        self._config.pop("admin_user_id", None)
        other_id, _ = self.create_user(nickname="Someone", email="s@example.com")
        comment_id = create_comment("post", other_id, "spam")
        response = self.app.request("POST", f"/article/post/comment/delete/{comment_id}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 403)

    def test_delete_comment_of_another_article_is_rejected(self):
        self.write_article("other", "body")
        self._post_comment("mine")
        comment_id = get_comments_by_article("post")[0]["id"]
        response = self.app.request("POST", f"/article/other/comment/delete/{comment_id}",
                                    form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 400)

    def test_malformed_comment_id_is_rejected(self):
        """非法 id 只能是 400/404/405，绝不能 500 或 302（不产生状态变更）。"""
        for raw, expected in (("abc", 400), ("-1", 404), ("1.5", 400),
                              ("", 405), ("99999999", 404)):
            with self.subTest(raw=raw):
                response = self.app.request("POST", f"/article/post/comment/delete/{raw}",
                                            form={"csrf_token": self.csrf}, cookies=self.cookies)
                self.assertEqual(response.status, expected)
                self.assertIn(response.status, (400, 404, 405))

    def test_orphan_comment_from_deleted_parent_still_renders(self):
        """父评论被物理删除后（ON DELETE SET NULL），子评论升级为顶层仍可见。"""
        self._post_comment("root")
        root_id = get_comments_by_article("post")[0]["id"]
        self._post_comment("child", reply_to=root_id)

        with get_connection() as conn:
            conn.execute("DELETE FROM comment WHERE id = ?", (root_id,))
            conn.commit()
        remaining = get_comments_by_article("post")
        self.assertEqual(len(remaining), 1)
        self.assertIsNone(remaining[0]["parent_id"])
        response = self.app.request("GET", "/article/post")
        self.assertIn("child", response.text)


class RateLimitTransactionTests(ElenvindTestCase):
    def test_try_post_comment_is_all_or_nothing(self):
        user_id, _ = self.create_user()
        outcome = try_post_comment("s", user_id, "1.2.3.4", "text", None,
                                   max_per_user=1, max_per_ip=1, window_seconds=60,
                                   max_per_article=1, created_at="2026-01-01T00:00:00")
        self.assertEqual(outcome, "ok")
        blocked = try_post_comment("s", user_id, "1.2.3.4", "text2", None,
                                   max_per_user=1, max_per_ip=1, window_seconds=60,
                                   max_per_article=1, created_at="2026-01-01T00:00:01")
        self.assertEqual(blocked, "rate_user")
        self.assertEqual(len(get_comments_by_article("s")), 1)


if __name__ == "__main__":
    unittest.main()
