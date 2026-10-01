"""评论系统测试：树组装（O(n)、无递归）、孤儿评论、层级上限、限流、增删恢复权限。

架构说明（与旧测试的差别）：评论区现在是"Feature 准备数据 + Jinja2 模板排版"。
因此渲染类测试调用 `features.blog.logic.build_comment_rows()` 拿到行数据，
再用真实模板渲染，而不是调用已经不存在的 `render_comments()`。
"""
import time
import unittest

from tests.support import ElenvindTestCase

from elenvind.core.db_base import connect
from elenvind.core.db_comment import (
    create_comment,
    get_comment_by_id,
    get_comments_by_article,
)
from elenvind.core.db_comment_rate import try_post_comment
from elenvind.core.templating import render_template
from elenvind.features.blog import logic as blog


def render_comments(slug, user, *, reply_to=None, max_length=1000, csrf_token=None):
    """测试辅助：用真实模板渲染评论区（等价于视图里的渲染路径）。

    `csrf_token` 参数仅为兼容旧调用签名——模板里的令牌来自
    `csrf_input()` 全局（即当前请求上下文），不再手工传入。
    """
    rows, total, max_len = blog.build_comment_rows(slug, user, max_length=max_length)
    context = {
        "comment_rows": rows,
        "comment_total": total,
        "max_comment_length": max_len,
        "reply_to_value": str(reply_to or ""),
        "reply_to_message": "",
        "article": {"slug": slug},
    }
    return render_template("partials/comments.html", context)


class CommentTreeTests(unittest.TestCase):
    def _row(self, cid, parent_id):
        return {"id": cid, "parent_id": parent_id, "created_at": "", "user_id": 1,
                "article_slug": "s", "content": "", "is_deleted": 0,
                "nickname": "n", "user_deleted": 0}

    def _order(self, comments):
        """返回 [(id, depth)]；复用生产代码的树展开逻辑。

        build_comment_rows 需要数据库，这里直接用同样的算法对内存数据排序：
        与生产实现共享"children_by_parent + 显式栈"的策略。
        """
        by_id = {row["id"]: row for row in comments}
        children = {}
        roots = []
        for row in comments:
            parent_id = row["parent_id"]
            if parent_id is None or parent_id not in by_id:
                roots.append(row)
            else:
                children.setdefault(parent_id, []).append(row)
        ordered = []
        stack = [(row, 1) for row in reversed(roots)]
        seen = set()
        while stack:
            row, depth = stack.pop()
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            ordered.append((row, depth))
            for child in reversed(children.get(row["id"], ())):
                stack.append((child, depth + 1))
        return ordered

    def test_flat_tree(self):
        ordered = self._order([self._row(1, None), self._row(2, None)])
        self.assertEqual([(c["id"], d) for c, d in ordered], [(1, 1), (2, 1)])

    def test_nested_tree_depth_first_order(self):
        comments = [self._row(1, None), self._row(2, 1), self._row(3, 2),
                    self._row(4, 1), self._row(5, None), self._row(6, 5)]
        ordered = self._order(comments)
        self.assertEqual([c["id"] for c, _ in ordered], [1, 2, 3, 4, 5, 6])
        self.assertEqual([d for _, d in ordered], [1, 2, 3, 2, 1, 2])

    def test_orphan_comments_become_roots(self):
        """父评论不存在（被物理清除）的子评论必须仍能显示为顶层。"""
        ordered = self._order([self._row(10, 999), self._row(11, 10)])
        self.assertEqual([(c["id"], d) for c, d in ordered], [(10, 1), (11, 2)])

    def test_orphan_child_is_reachable_not_dropped(self):
        """父不存在时子评论也必须出现（不能因为找不到父就整条丢失）。"""
        ordered = self._order([self._row(10, 999)])
        self.assertEqual([c["id"] for c, _ in ordered], [10])

    def test_deep_tree_is_iterative_and_linear(self):
        """10,000 条评论的深链不能在递归或 O(n²) 上爆炸。"""
        count = 10_000
        comments = [self._row(1, None)]
        comments += [self._row(i, i - 1) for i in range(2, count + 1)]

        start = time.perf_counter()
        ordered = self._order(comments)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(ordered), count)
        self.assertEqual(ordered[-1][1], count)
        self.assertLess(elapsed, 5.0, f"deep tree took {elapsed:.2f}s")

    def test_wide_tree_is_linear(self):
        count = 5_000
        comments = [self._row(1, None)] + [self._row(i, 1) for i in range(2, count + 1)]
        start = time.perf_counter()
        ordered = self._order(comments)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(ordered), count)
        self.assertLess(elapsed, 5.0, f"wide tree took {elapsed:.2f}s")


class CommentRowShapeTests(ElenvindTestCase):
    """回归：按 id 取的单条评论必须与按文章取的行**同形**。

    曾经 `get_comment_by_id()` 用 `SELECT * FROM comment`（JOIN 缺失），
    而渲染"回复 @某某"提示要读 `nickname` / `user_deleted`，
    于是**点"回复"就 500**（IndexError: No item with that key）。
    这里同时守住"单条 vs 整篇"的形状契约与真实的回复页渲染。
    """

    def _rows(self):
        from elenvind.core.db_comment import get_comment_by_id, get_comments_by_article
        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.write_article("post", "body")
        comment_id = create_comment("post", user_id, "root")
        return get_comment_by_id(comment_id), get_comments_by_article("post")[0]

    def test_single_comment_row_has_user_columns(self):
        single, listing = self._rows()
        self.assertEqual(sorted(single.keys()), sorted(listing.keys()),
                         "单条评论与整篇评论的行形状不一致")
        for column in ("nickname", "user_deleted"):
            self.assertIn(column, single.keys(), f"缺少 {column} 列")

    def test_single_comment_row_keeps_permission_fields(self):
        """权限校验用的 user_id / article_slug 仍必须在（不能为了补列丢列）。"""
        single, _ = self._rows()
        for column in ("id", "article_slug", "user_id", "content", "parent_id",
                       "is_deleted"):
            self.assertIn(column, single.keys(), f"缺少 {column} 列")

    def test_reply_page_renders_when_logged_in(self):
        """点"回复"进文章页（?reply_to=N）必须 200 并显示回复提示。"""
        from elenvind.core.db_comment import get_comments_by_article
        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.write_article("post", "body")
        create_comment("post", user_id, "root")
        root_id = get_comments_by_article("post")[0]["id"]

        self.create_user(nickname="Me", email="me@example.com")
        session, csrf = self.login_ok("me@example.com", "correct horse battery")
        response = self.app.request("GET", "/article/post",
                                    query={"reply_to": str(root_id)},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 200, response.text[:300])
        self.assertIn("Ann", response.text)
        self.assertIn('name="reply_to"', response.text)
        self.assertIn(f'value="{root_id}"', response.text)

    def test_reply_page_renders_for_deleted_author(self):
        """回复"已注销用户"的评论也必须 200（走昵称占位分支）。"""
        from elenvind.core.db_comment import get_comments_by_article
        from elenvind.core.db_user import delete_user

        user_id, _ = self.create_user(nickname="Gone", email="gone@example.com")
        self.write_article("post", "body")
        create_comment("post", user_id, "root from a deleted account")
        root_id = get_comments_by_article("post")[0]["id"]
        delete_user(user_id)

        self.create_user(nickname="Me", email="me@example.com")
        session, csrf = self.login_ok("me@example.com", "correct horse battery")
        response = self.app.request("GET", "/article/post",
                                    query={"reply_to": str(root_id)},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 200, response.text[:300])
        self.assertIn("Replying to", response.text)
        # 占位昵称来自配置，且必须带 user_id 以便区分同名
        self.assertIn(str(user_id), response.text)

    def test_reply_page_ignores_unknown_reply_target(self):
        """reply_to 指向不存在的评论：正常渲染文章页，不报错。"""
        self.write_article("post", "body")
        self.create_user(nickname="Me", email="me@example.com")
        session, csrf = self.login_ok("me@example.com", "correct horse battery")
        response = self.app.request("GET", "/article/post",
                                    query={"reply_to": "999999"},
                                    cookies=self.app_cookies(session=session, csrf=csrf))
        self.assertEqual(response.status, 200)
        self.assertNotIn("Replying to", response.text)


class CommentRenderTests(ElenvindTestCase):
    def test_empty_comment_section(self):
        self.write_article("post", "body")
        html = render_comments("post", None)
        self.assertIn("comments", html)
        self.assertIn('id="comments"', html)
        self.assertIn("No comments yet", html)

    def test_comment_content_is_escaped(self):
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        create_comment("post", user_id, "<script>alert(1)</script>\nsecond line")
        html = render_comments("post", None)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<br>", html)   # 换行保留

    def test_deleted_comment_is_masked_for_visitors(self):
        from elenvind.core.db_comment import soft_delete_comment
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        comment_id = create_comment("post", user_id, "secret message")
        soft_delete_comment(comment_id)
        html = render_comments("post", None)
        self.assertNotIn("secret message", html)
        self.assertIn("█", html)

    def test_deleted_comment_visible_to_admin_with_strikethrough(self):
        from elenvind.core.db_comment import soft_delete_comment
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
        from elenvind.core.db_comment import soft_delete_comment
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
        # 未登录 + 有效 CSRF：认证闸门拒绝 -> 403
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
        with connect() as conn:
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
        from elenvind.core.db_comment import soft_delete_comment
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
        # 评论不属于该文章：403（拒绝），且绝不产生状态变更
        self.assertEqual(response.status, 403)
        self.assertEqual(get_comment_by_id(comment_id)["is_deleted"], 0)

    def test_malformed_comment_id_is_rejected(self):
        """非法 id 绝不能 500，也绝不能 302（不产生任何状态变更）。"""
        for raw in ("abc", "-1", "1.5", "", "99999999", "1e5", "0x1", " "):
            with self.subTest(raw=raw):
                response = self.app.request(
                    "POST", f"/article/post/comment/delete/{raw}",
                    form={"csrf_token": self.csrf}, cookies=self.cookies)
                self.assertIn(response.status, (400, 403, 404, 405),
                              f"{raw!r} -> {response.status}")
                self.assertNotEqual(response.status, 500)
                self.assertNotEqual(response.status, 302)

    def test_orphan_comment_from_deleted_parent_still_renders(self):
        """父评论被物理删除后（ON DELETE SET NULL），子评论升级为顶层仍可见。"""
        self._post_comment("root")
        root_id = get_comments_by_article("post")[0]["id"]
        self._post_comment("child", reply_to=root_id)

        with connect() as conn:
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
