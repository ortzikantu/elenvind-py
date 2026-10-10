"""评论契约：转义、深度硬约束、文章配额（涂黑即释放）、越权。

回归重点（本次修复）：`max_comments_per_article` 只统计**未涂黑**的评论。
修复前涂黑只是把 `is_deleted` 置 1 而配额照旧计入，于是一篇被刷满的文章
永久拒绝新评论 —— 连管理员也救不回来（全项目没有 DELETE 评论的路径）。
"""
from __future__ import annotations

import unittest

from tests import support
from tests.support import ARTICLE_SLUG


class CommentBasics(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()
        self.user = support.Session()
        self.assertEqual(self.user.create_account(
            "Alice", "alice@example.test", "correct-horse-1").status, 302)

    def test_comment_body_is_escaped(self):
        payload = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'
        self.assertEqual(self.user.comment(ARTICLE_SLUG, payload).status, 302)
        page = self.user.get(f"/article/{ARTICLE_SLUG}").text()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<img src=x onerror", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_empty_and_overlong_are_rejected(self):
        self.assertEqual(self.user.comment(ARTICLE_SLUG, "   ").status, 400)
        self.assertEqual(self.user.comment(ARTICLE_SLUG, "y" * 1001).status, 400)
        self.assertEqual(self.user.comment(ARTICLE_SLUG, "y" * 1000).status, 302)

    def test_invalid_reply_target_is_rejected(self):
        self.assertEqual(self.user.comment(ARTICLE_SLUG, "hi", reply_to=99999).status, 400)

    def test_comment_on_missing_article_is_404(self):
        result = self.user.post("/article/no-such-slug/comment",
                                {"content": "hi", "csrf_token": self.user.csrf("/login")})
        self.assertEqual(result.status, 404)

    def test_anonymous_post_is_stopped_by_the_auth_gate(self):
        anonymous = support.Session()
        token = anonymous.csrf("/login")           # 匿名也能拿到 CSRF 令牌
        result = anonymous.post(f"/article/{ARTICLE_SLUG}/comment",
                               {"content": "hi", "csrf_token": token})
        self.assertEqual(result.status, 403)
        self.assertEqual(support.query("SELECT COUNT(*) FROM comment")[0][0], 0)


class CommentDepth(unittest.TestCase):
    """`max_comment_depth = 3`（测试配置）：第 4 层必须被事务内的权威判定拒绝。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        self.user = support.Session()
        self.user.create_account("Alice", "alice@example.test", "correct-horse-1")

    def test_depth_limit_is_enforced_server_side(self):
        parent = None
        for depth in range(1, 4):
            self.assertEqual(self.user.comment(ARTICLE_SLUG, f"depth-{depth}", parent).status,
                             302, f"depth {depth} should be allowed")
            parent = support.query("SELECT MAX(id) FROM comment")[0][0]
        denied = self.user.comment(ARTICLE_SLUG, "depth-4", parent)
        self.assertEqual(denied.status, 400)
        self.assertIn("depth", denied.text().lower())
        self.assertEqual(support.query("SELECT COUNT(*) FROM comment")[0][0], 3)


class CommentQuota(unittest.TestCase):
    """文章配额：涂黑释放名额（本次修复的核心行为）。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        self.user = support.Session()
        self.user.create_account("Alice", "alice@example.test", "correct-horse-1")

    def test_quota_is_released_by_redaction(self):
        config = support.live_config()
        original = config.get("max_comments_per_article")
        config["max_comments_per_article"] = 2
        try:
            self.assertEqual(self.user.comment(ARTICLE_SLUG, "one").status, 302)
            self.assertEqual(self.user.comment(ARTICLE_SLUG, "two").status, 302)
            denied = self.user.comment(ARTICLE_SLUG, "three")
            self.assertEqual(denied.status, 429, "the article must be full")

            first = support.query("SELECT MIN(id) FROM comment")[0][0]
            self.assertEqual(self.user.delete_comment(ARTICLE_SLUG, first).status, 302)
            self.assertEqual(
                support.query("SELECT is_deleted FROM comment WHERE id=?", (first,))[0][0], 1)
            # 行还在（评论树不散架），但不再占配额
            self.assertEqual(support.query("SELECT COUNT(*) FROM comment")[0][0], 2)
            self.assertEqual(self.user.comment(ARTICLE_SLUG, "three").status, 302)
        finally:
            config["max_comments_per_article"] = original


class CommentAuthorization(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()
        self.owner = support.Session()
        self.owner.create_account("Alice", "alice@example.test", "correct-horse-1")
        self.owner.comment(ARTICLE_SLUG, "mine")
        self.comment_id = support.query("SELECT MIN(id) FROM comment")[0][0]
        self.other = support.Session()
        self.other.create_account("Bob", "bob@example.test", "correct-horse-2")

    def test_other_user_cannot_redact(self):
        denied = self.other.delete_comment(ARTICLE_SLUG, self.comment_id)
        self.assertEqual(denied.status, 403)
        self.assertEqual(
            support.query("SELECT is_deleted FROM comment WHERE id=?",
                          (self.comment_id,))[0][0], 0)

    def test_other_user_cannot_edit(self):
        result = self.other.post(
            f"/article/{ARTICLE_SLUG}/comment/edit/{self.comment_id}",
            {"content": "hijacked", "csrf_token": self.other.csrf(f"/article/{ARTICLE_SLUG}")})
        self.assertEqual(result.status, 403)
        self.assertEqual(support.query("SELECT content FROM comment WHERE id=?",
                                       (self.comment_id,))[0][0], "mine")

    def test_owner_can_redact_and_edit(self):
        self.assertEqual(self.other.delete_comment(ARTICLE_SLUG, self.comment_id).status, 403)
        result = self.owner.post(
            f"/article/{ARTICLE_SLUG}/comment/edit/{self.comment_id}",
            {"content": "edited", "csrf_token": self.owner.csrf(f"/article/{ARTICLE_SLUG}")})
        self.assertEqual(result.status, 302)
        self.assertEqual(support.query("SELECT content FROM comment WHERE id=?",
                                       (self.comment_id,))[0][0], "edited")
        self.assertEqual(self.owner.delete_comment(ARTICLE_SLUG, self.comment_id).status, 302)
        self.assertEqual(
            support.query("SELECT is_deleted FROM comment WHERE id=?",
                          (self.comment_id,))[0][0], 1)

    def test_cross_article_comment_id_is_rejected(self):
        result = self.other.post(
            f"/article/2026-01-02-second/comment/delete/{self.comment_id}",
            {"csrf_token": self.other.csrf("/article/2026-01-02-second")})
        self.assertEqual(result.status, 403)


if __name__ == "__main__":
    unittest.main()
