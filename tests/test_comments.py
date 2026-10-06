"""评论系统测试：树组装（O(n)、无递归）、孤儿评论、层级上限、限流、增删恢复权限。

架构说明（与旧测试的差别）：评论区现在是"模块准备数据 + Jinja2 模板排版"。
因此渲染类测试调用 `modules.blog.logic.build_comment_rows()` 拿到行数据，
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
from elenvind.modules.blog import logic
from elenvind.modules.blog import logic as blog


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
    """评论树展开。

    **测试调用生产实现** `core.db_comment.flatten_comment_tree`，不再复制算法。

    这里曾经有一份 `_order()` 副本（"build_comment_rows 需要数据库，所以
    自己写一份同样的算法"）—— 结果是两份实现慢慢分叉，而且测试测的是副本：

    - 副本用 `if parent_id is None or parent_id not in by_id` 判根，
      生产实现当时也是这么写的，于是**两边的环处理都不对**：
      root(1) + 环 2<->3 时两边都只渲染出 [1]，评论 2、3 从页面上凭空消失；
    - 副本还多了一个 `seen` 集合，看起来"已经防环了"，掩盖了真正的缺陷。
    - 而 `comment_depth`（层级计算）早就带了 seen 集合 —— 同一份坏数据，
      一处防了、另一处没防。

    所以修法是：把展开收进 Core 只留一份，测试直接调它。
    """

    def _row(self, cid, parent_id):
        return {"id": cid, "parent_id": parent_id, "created_at": "", "user_id": 1,
                "article_slug": "s", "content": "", "is_deleted": 0,
                "nickname": "n", "user_deleted": 0}

    def _order(self, comments):
        """返回 [(row, depth)]，走生产实现。"""
        from elenvind.core.db_comment import flatten_comment_tree

        return flatten_comment_tree(comments)

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

    # ---------- 环（坏数据）----------

    def test_cycle_reachable_from_root_does_not_vanish(self):
        """回归：环上的评论曾经被整条丢弃。

        root(1) 是真根；2 的父是 1；然后 3 的父是 2、2 的父被改成 3
        （手工 SQL / 库外脚本能造出这种数据）。
        旧实现下 2 与 3 "父存在但不可达"，既不是根也没有从根进来的边，
        于是渲染结果只剩 [1] —— 评论 2、3 在页面上**完全消失**。
        """
        comments = [self._row(1, None), self._row(2, 3), self._row(3, 2)]
        ordered = self._order(comments)
        self.assertEqual(sorted(c["id"] for c, _ in ordered), [1, 2, 3],
                         "环上的评论从页面上消失了")

    def test_cycle_without_any_root_does_not_vanish(self):
        """整张表都在环里（没有任何 parent IS NULL）时也不能全部消失。"""
        comments = [self._row(1, 2), self._row(2, 1)]
        ordered = self._order(comments)
        self.assertEqual(sorted(c["id"] for c, _ in ordered), [1, 2])

    def test_cycle_members_appear_exactly_once(self):
        """环上的每条评论只能出现一次（不能无限展开）。"""
        comments = [self._row(1, None), self._row(2, 3), self._row(3, 2)]
        ordered = self._order(comments)
        ids = [c["id"] for c, _ in ordered]
        self.assertEqual(len(ids), len(set(ids)), f"有重复：{ids}")

    def test_self_referencing_comment_does_not_vanish_or_loop(self):
        """自己指向自己（parent_id == id）是退化环，也要能渲染。"""
        comments = [self._row(1, None), self._row(2, 2)]
        ordered = self._order(comments)
        self.assertEqual(sorted(c["id"] for c, _ in ordered), [1, 2])

    def test_every_stored_comment_is_rendered_under_any_shape(self):
        """不变式：无论父子关系多畸形，库里的评论一条都不能少。"""
        import random

        rng = random.Random(20260101)
        for trial in range(60):
            size = rng.randint(1, 12)
            comments = []
            for cid in range(1, size + 1):
                # 随机指向任意 id（含自己），制造环、孤儿、深链
                parent = rng.choice([None] + list(range(1, size + 1)))
                comments.append(self._row(cid, parent))
            with self.subTest(trial=trial):
                ordered = self._order(comments)
                self.assertEqual(sorted(c["id"] for c, _ in ordered),
                                 list(range(1, size + 1)),
                                 f"有评论没被渲染：{[(r['id'], r['parent_id']) for r in comments]}")

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


class CommentTreeEndToEndTests(ElenvindTestCase):
    """端到端：坏数据的环不得让评论从**页面上**消失。

    上面 CommentTreeTests 测的是 Core 的纯函数；这里走真实 HTTP 渲染路径
    （`build_comment_rows` -> 模板），确认整条链路上都不会丢评论。
    """

    def _make_cycle(self):
        """构造 root(1) + 环 2<->3，并返回真实 id。"""
        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.write_article("post", "body")
        root = create_comment("post", user_id, "root comment")
        first = create_comment("post", user_id, "cycle A", root)
        second = create_comment("post", user_id, "cycle B", first)
        with connect() as conn:
            # 手工把 first 的父改成 second：制造 first <-> second 的环
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                         (second, first))
            conn.commit()
        return root, first, second

    def test_cycle_members_are_rendered_on_the_article_page(self):
        root, first, second = self._make_cycle()
        response = self.app.request("GET", "/article/post")
        self.assertEqual(response.status, 200)
        for label in ("root comment", "cycle A", "cycle B"):
            with self.subTest(comment=label):
                self.assertIn(label, response.text,
                              f"评论 {label!r} 没有渲染到页面上（坏数据让它消失了）")

    def test_cycle_members_are_actionable(self):
        """环上的评论也必须能删（否则用户既看不到也清不掉）。"""
        root, first, second = self._make_cycle()
        admin = self.create_user(nickname="Admin", email="admin@example.com")
        session, csrf = self.login_ok("ann@example.com", "correct horse battery")
        jar = self.app_cookies(session=session, csrf=csrf)
        # 作者本人删除自己那条环上的评论
        response = self.app.request(
            "POST", f"/article/post/comment/delete/{first}",
            form={"csrf_token": csrf}, cookies=jar)
        self.assertEqual(response.status, 302)
        row = get_comment_by_id(first)
        self.assertEqual(row["is_deleted"], 1)

    def test_all_stored_comments_appear_on_page(self):
        """不变式（端到端）：库里有多少条评论，页面上就该有多少条。"""
        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.write_article("post", "body")
        ids = [create_comment("post", user_id, f"c{index}")
               for index in range(5)]
        with connect() as conn:
            # 造畸形父子关系：环（0<->1）、自指（2）、深链（3->4）
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                         (ids[1], ids[0]))
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                         (ids[0], ids[1]))
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                         (ids[2], ids[2]))
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                         (ids[4], ids[3]))
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?",
                         (ids[3], ids[4]))
            conn.commit()

        response = self.app.request("GET", "/article/post")
        self.assertEqual(response.status, 200)
        for index in range(5):
            with self.subTest(comment=index):
                self.assertIn(f"c{index}", response.text,
                              f"评论 c{index} 丢失")

    def test_orphan_from_on_delete_set_null_still_renders(self):
        """孤儿评论的真实来源：父行被物理删除时 `ON DELETE SET NULL` 把子评论升级。

        这条路径**能**在正常运维里出现（库外手工清理父行），因此必须保住。
        """
        user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.write_article("post", "body")
        parent = create_comment("post", user_id, "parent text")
        child = create_comment("post", user_id, "child text", parent)
        with connect() as conn:
            conn.execute("DELETE FROM comment WHERE id = ?", (parent,))
            conn.commit()
        row = get_comment_by_id(child)
        self.assertIsNone(row["parent_id"], "ON DELETE SET NULL 没有生效")
        response = self.app.request("GET", "/article/post")
        self.assertIn("child text", response.text,
                      "父行被删后子评论没有升级为顶层（评论丢失）")


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

    def test_reply_to_soft_deleted_comment_is_allowed(self):
        """软删除可恢复，因此已删除评论仍可被回复（关系链保持完整）。"""
        from elenvind.core.db_comment import soft_delete_comment
        self._post_comment("root")
        root_id = get_comments_by_article("post")[0]["id"]
        soft_delete_comment(root_id)
        response = self._post_comment("reply", reply_to=root_id)
        self.assertEqual(response.status, 302, response.text[:300])
        comments = get_comments_by_article("post")
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[1]["parent_id"], root_id)
        self.assertEqual(comments[1]["content"], "reply")

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


class ParentIdValidationTests(ElenvindTestCase):
    """父评论校验必须在写入事务内完成（`try_post_comment`）。

    背景：`parent_id` 曾经原样透传到 INSERT，可以写入不存在的父评论、
    也可以跨文章引用别的评论 id，产生孤儿/错链。校验放在
    BEGIN IMMEDIATE 之后、INSERT 之前，因此与限流计数同一事务。
    """

    LIMITS = dict(max_per_user=100, max_per_ip=100, window_seconds=60,
                  max_per_article=100)

    #: 自增序号：让每条评论的 created_at 严格递增。
    #: `get_comments_by_article` 按 (created_at, id) 排序，固定时间戳会让
    #: "新写的那条排在哪"变得不确定（测试曾经因此误判）。
    _seq = 0

    def _post(self, slug, user_id, parent_id, content="x", **overrides):
        limits = dict(self.LIMITS)
        limits.update(overrides)
        ParentIdValidationTests._seq += 1
        at = f"2026-01-01T00:00:{ParentIdValidationTests._seq:02d}"
        return try_post_comment(slug, user_id, "1.2.3.4", content, parent_id,
                                created_at=at, **limits)

    def _rate_rows(self):
        with connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM comment_rate").fetchone()[0]

    def test_nonexistent_parent_returns_bad_parent(self):
        user_id, _ = self.create_user()
        self.write_article("post-a", "body")
        self.assertEqual(self._post("post-a", user_id, 99999), "bad_parent")
        self.assertEqual(get_comments_by_article("post-a"), [])
        # 被拒绝时不写限流流水（否则拒绝本身会放大表增长）
        self.assertEqual(self._rate_rows(), 0)

    def test_cross_article_parent_returns_bad_parent(self):
        user_id, _ = self.create_user()
        self.write_article("post-a", "body")
        self.write_article("post-b", "body")
        other = create_comment("post-b", user_id, "belongs to B")
        self.assertEqual(self._post("post-a", user_id, other), "bad_parent")
        self.assertEqual(len(get_comments_by_article("post-a")), 0)
        self.assertEqual(len(get_comments_by_article("post-b")), 1)
        self.assertEqual(self._rate_rows(), 0)

    @staticmethod
    def _by_content(slug, content):
        """按内容取评论行：不依赖列表顺序（时间戳来源不同，顺序不可依赖）。"""
        for row in get_comments_by_article(slug):
            if row["content"] == content:
                return row
        return None

    def test_soft_deleted_parent_is_accepted(self):
        """本项目的删除是软删除、可恢复，因此已删除评论仍可被回复。"""
        from elenvind.core.db_comment import soft_delete_comment
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        root = create_comment("post", user_id, "root")
        soft_delete_comment(root)
        self.assertTrue(get_comment_by_id(root)["is_deleted"])

        self.assertEqual(self._post("post", user_id, root, "reply to deleted"), "ok")
        reply = self._by_content("post", "reply to deleted")
        self.assertIsNotNone(reply, get_comments_by_article("post"))
        self.assertEqual(reply["parent_id"], root)

    def test_valid_parent_returns_ok(self):
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        root = create_comment("post", user_id, "root")
        self.assertEqual(self._post("post", user_id, root, "reply"), "ok")
        reply = self._by_content("post", "reply")
        self.assertIsNotNone(reply, get_comments_by_article("post"))
        self.assertEqual(reply["parent_id"], root)
        self.assertEqual(len(get_comments_by_article("post")), 2)
        self.assertEqual(self._rate_rows(), 1)

    def test_top_level_comment_still_works(self):
        """parent_id=None 不触发校验（顶层评论路径不受影响）。"""
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        self.assertEqual(self._post("post", user_id, None), "ok")
        self.assertIsNone(get_comments_by_article("post")[0]["parent_id"])

    def test_rate_limit_still_wins_over_parent_check(self):
        """既有三个 outcome 的判定顺序不变：限流先于父评论校验。"""
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        self.assertEqual(self._post("post", user_id, None, max_per_user=1), "ok")
        # 父评论不存在 + 已超限 -> 仍应返回 rate_user（顺序不变）
        self.assertEqual(self._post("post", user_id, 99999, max_per_user=1),
                         "rate_user")

    def test_bad_parent_rolls_back_inside_transaction(self):
        """校验失败必须 rollback：限流流水与评论都不留痕。"""
        user_id, _ = self.create_user()
        self.write_article("post", "body")
        for index in range(3):
            self.assertEqual(self._post("post", user_id, 424242,
                                        content=f"x{index}"), "bad_parent")
        self.assertEqual(self._rate_rows(), 0)
        self.assertEqual(get_comments_by_article("post"), [])


class ParentIdHttpTests(ElenvindTestCase):
    """HTTP 层：非法父评论被拒且库中无新行。"""

    def setUp(self):
        super().setUp()
        self.write_article("post-a", "body")
        self.write_article("post-b", "body")
        self.create_user(nickname="Ann", email="ann@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com",
                                                "correct horse battery")
        self.cookies = self.app_cookies(session=self.session, csrf=self.csrf)

    def _post(self, reply_to, slug="post-a"):
        form = {"csrf_token": self.csrf, "content": "hello"}
        if reply_to is not None:
            form["reply_to"] = str(reply_to)
        return self.app.request("POST", f"/article/{slug}/comment",
                                form=form, cookies=self.cookies)

    def test_nonexistent_parent_is_rejected_without_writing(self):
        response = self._post(99999)
        self.assertEqual(response.status, 400)
        self.assertEqual(get_comments_by_article("post-a"), [])

    def test_cross_article_parent_is_rejected_without_writing(self):
        other = create_comment("post-b", 1, "other article")
        response = self._post(other)
        self.assertEqual(response.status, 400)
        self.assertEqual(get_comments_by_article("post-a"), [])

    def test_reply_to_soft_deleted_comment_succeeds(self):
        """回归：曾经**拒绝**回复已删除评论，与"软删除可恢复"的语义冲突。"""
        from elenvind.core.db_comment import soft_delete_comment
        root = create_comment("post-a", 1, "root")
        soft_delete_comment(root)
        response = self._post(root)
        self.assertEqual(response.status, 302, response.text[:300])
        comments = get_comments_by_article("post-a")
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[1]["parent_id"], root)

    def test_valid_reply_succeeds(self):
        root = create_comment("post-a", 1, "root")
        response = self._post(root)
        self.assertEqual(response.status, 302)
        self.assertEqual(len(get_comments_by_article("post-a")), 2)

    def test_bad_parent_outcome_has_user_facing_message(self):
        """bad_parent 必须映射出可读文案，而不是空字符串。

        注意 HTTP 层对"不存在的父评论"会先用友好预检拦下，因此这里直接验证
        `logic.post_comment` 对 bad_parent 的文案映射（可靠、不依赖日志）。
        """
        outcome, message = logic.post_comment("post-a", 1, "1.2.3.4", "hi", 99999)
        self.assertEqual(outcome, "bad_parent")
        self.assertTrue(message)
        self.assertIn("no longer exists", message)

    def test_orphan_comment_is_never_created(self):
        """遍历一串不存在的 parent_id，库里始终没有新行。"""
        for parent in (1, 2, 999, 12345):
            self._post(parent)
        self.assertEqual(get_comments_by_article("post-a"), [])


class CommentContentRenderingTests(unittest.TestCase):
    """`_comment_content` 的渲染契约（全项目唯一的原始 HTML 注入点）。

    这里刻意用**逐字节**断言：评论不走 markdown 白名单净化器，
    这个函数的输出直接进 HTML，任何字符变化都可能是安全问题。
    """

    def render(self, content, *, is_deleted=0, admin=False):
        from elenvind.modules.blog.logic import _comment_content
        return str(_comment_content({"content": content, "is_deleted": is_deleted},
                                    admin))

    def test_returns_markup_not_str(self):
        """必须是 Markup，否则模板会把它再转义一遍（页面上看到标签字面量）。"""
        from markupsafe import Markup
        from elenvind.modules.blog.logic import _comment_content
        result = _comment_content({"content": "x", "is_deleted": 0}, False)
        self.assertIsInstance(result, Markup)

    def test_escapes_raw_html(self):
        for hostile in ("<b>x</b>", "<script>alert(1)</script>",
                        '<img src=x onerror="alert(1)">', "a & b", "a &amp; b"):
            with self.subTest(content=hostile):
                out = self.render(hostile)
                self.assertNotIn("<b>", out)
                self.assertNotIn("<script", out)
                self.assertNotIn("<img", out)
                self.assertNotIn('onerror="', out.replace("&quot;", ""))

    def test_newline_becomes_br_not_escaped(self):
        """转义顺序：先转义再换 <br>，不能反过来（否则会看到 &lt;br&gt;）。"""
        out = self.render("a\nb")
        self.assertIn("a<br>b", out)
        self.assertNotIn("&lt;br&gt;", out)

    def test_crlf_and_cr_are_normalized(self):
        """三种换行风格必须渲染成同样的结果。"""
        expected = self.render("a\nb")
        self.assertEqual(self.render("a\r\nb"), expected)
        self.assertEqual(self.render("a\rb"), expected)

    def test_deleted_comment_masks_with_blocks(self):
        out = self.render("abcd", is_deleted=1, admin=False)
        self.assertIn("█" * 4, out)
        self.assertNotIn("abcd", out)

    def test_deleted_mask_width_counts_normalized_text(self):
        """打码方块数按**规范化后**的长度算，CRLF 不能算成两行。"""
        for content in ("a\nb", "a\r\nb", "a\rb"):
            with self.subTest(content=content):
                out = self.render(content, is_deleted=1, admin=False)
                self.assertEqual(out.count("█"), 3)

    def test_admin_sees_deleted_content_with_strikethrough_class(self):
        out = self.render("<b>x</b>", is_deleted=1, admin=True)
        self.assertIn("is-deleted", out)
        self.assertIn("&lt;b&gt;", out)          # 仍然转义
        self.assertNotIn("█", out)               # 管理员不打码

    def test_empty_content_renders_empty_span(self):
        out = self.render("")
        self.assertIn('<span class="comment-content"></span>', out)

    def test_normalize_newlines_helper_is_idempotent(self):
        from elenvind.modules.blog.logic import _normalize_newlines
        for value in ("a\r\nb", "a\rb", "a\nb", "a\r\n\r\nb", ""):
            with self.subTest(value=value):
                once = _normalize_newlines(value)
                self.assertEqual(_normalize_newlines(once), once)
                self.assertNotIn("\r", once)


class CommentRateTransactionGuardTests(unittest.TestCase):
    """结构性守卫：发布事务里不得再做流水清理。

    `DELETE FROM comment_rate` 是 O(表大小) 的操作，放在 `BEGIN IMMEDIATE`
    之后会拉长写锁持有时间（高并发下互相排队）。

    流水清理分两层（见 `core/db_prune.py`）：
    - **启动时**全量清一次（`cleanup_old_comment_attempts`）；
    - **运行期**在发布**提交之后**做机会式清理（默认每小时最多一次），
      因此既不占写锁，长跑进程也不会无界增长。

    也就是说：不在**事务内**删，但确实会在**事务外**删 ——
    本守卫只禁止前者。
    """

    @classmethod
    def setUpClass(cls):
        import pathlib
        from tests.support import PROJECT_ROOT
        cls.source = (PROJECT_ROOT / "elenvind" / "core"
                      / "db_comment_rate.py").read_text(encoding="utf-8")

    def _function_body(self, name):
        import re
        body = self.source.split(f"def {name}", 1)[1]
        return re.split(r"\ndef ", body)[0]

    def test_try_post_comment_has_no_delete(self):
        import re
        body = self._function_body("try_post_comment")
        self.assertEqual(re.findall(r"DELETE\s+FROM", body, re.I), [],
                         "发布事务里不应再有 DELETE（会拉长写锁持有时间）")

    def test_prune_happens_outside_the_write_transaction(self):
        """机会式清理必须在写事务**之外**调用（提交之后、锁已释放）。

        旧写法断言 "`conn.commit()` 在 `prune(` 之前"；C0 之后提交由
        `write_tx()` 统一负责，函数里不再有 `conn.commit()`。等价且更强的判定
        是结构性的：`prune(` 调用**不能落在 `with write_tx()` 的语句体里** ——
        否则它会在写事务内部再取一次 flock（重入守卫会报错，否则就是自死锁）。
        """
        import ast

        tree = ast.parse(self.source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "try_post_comment")
        guarded_ranges = []
        for node in ast.walk(function):
            if isinstance(node, ast.With) and any(
                    isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "id", "") == "write_tx"
                    for item in node.items):
                guarded_ranges.append((node.lineno, node.end_lineno))
        self.assertTrue(guarded_ranges, "try_post_comment 里找不到 write_tx() 块")

        prune_lines = [node.lineno for node in ast.walk(function)
                       if isinstance(node, ast.Call)
                       and getattr(node.func, "id", "") == "prune"]
        self.assertTrue(prune_lines, "发布后没有机会式清理，长跑进程会无界增长")
        for line in prune_lines:
            for start, end in guarded_ranges:
                self.assertFalse(start <= line <= end,
                                 "prune 在 write_tx() 块内调用 —— 会嵌套取锁/延长写锁")

    def test_startup_cleanup_still_deletes(self):
        body = self._function_body("cleanup_old_comment_attempts")
        self.assertIn("DELETE FROM comment_rate", body)

    def test_retention_constant_still_used(self):
        """保留期常量必须仍被启动清理引用（别把清理一起删掉了）。"""
        self.assertIn("RETENTION_DAYS", self.source)

    def test_no_delete_when_rejecting(self):
        """被拒绝的分支同样不应有清理动作，且每个 outcome 都在。"""
        body = self._function_body("try_post_comment")
        for outcome in ("rate_user", "rate_ip", "too_many", "bad_parent"):
            with self.subTest(outcome=outcome):
                self.assertIn(f'return "{outcome}"', body)
        # 拒绝路径只允许 rollback + return，不允许附带任何写操作
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("INSERT", "DELETE", "UPDATE")):
                self.assertNotIn("rate_", stripped,
                                 f"拒绝路径附近出现了写流水: {stripped}")


class SingleRawHtmlInjectionPointTests(unittest.TestCase):
    """守卫：全项目唯一的原始 HTML 注入点必须带有醒目注释。"""

    def test_warning_comment_present(self):
        import pathlib
        from tests.support import PROJECT_ROOT
        source = (PROJECT_ROOT / "elenvind" / "modules" / "blog"
                  / "logic.py").read_text(encoding="utf-8")
        self.assertIn("此处是全项目唯一的原始 HTML 注入点，改动前务必确认转义顺序",
                      source)

    def test_only_comment_content_injects_raw_html(self):
        """blog/logic.py 里只能用 Markup 包两处业务文本（评论正文的两个分支）。

        文章正文走 core.markdown 的白名单净化器，不在这里。
        出现新的 `Markup(...)` 调用就必须重新审视转义顺序。

        计数不含 `from markupsafe import Markup`（那不是调用），
        因此上限恰好是 2 —— 不给自己留"再多一处也没关系"的余量。
        """
        import pathlib
        import re
        from tests.support import PROJECT_ROOT
        source = (PROJECT_ROOT / "elenvind" / "modules" / "blog"
                  / "logic.py").read_text(encoding="utf-8")
        # 去掉注释与文档字符串后统计 Markup( 的调用
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        code = re.sub(r"^\s*#.*$", "", code, flags=re.M)
        calls = re.findall(r"^.*\bMarkup\(.*$", code, re.M)
        self.assertEqual(
            len(calls), 2,
            f"Markup() 调用数变了（{len(calls)}），新增注入点必须重新确认转义：\n"
            + "\n".join(line.strip() for line in calls))


class ReplyDepthWriteSideTests(ElenvindTestCase):
    """写入侧的深度上限（`max_comment_depth`）必须真正生效。

    背景：模板只在 `depth < depth_limit` 时渲染回复按钮，但 `reply_to` 是表单字段，
    可以直接构造。路由层虽然有预检，但它在 `BEGIN IMMEDIATE` **之前**——
    既能被绕过（直接调 core），也存在 TOCTOU（并发时两个请求可能都看到"还没到顶"）。
    因此权威判定放在 `try_post_comment` 的事务内，与 INSERT 同事务。
    """

    LIMITS = dict(max_per_user=1000, max_per_ip=1000, window_seconds=60,
                  max_per_article=1000)

    def setUp(self):
        super().setUp()
        self._config["max_comment_depth"] = 3
        self.user_id, _ = self.create_user(email="deep@example.com")
        self.write_article("post", "body")

    def _chain(self, length, user_id=None, slug="post"):
        """建一条 length 层的链，返回各层 id（用 create_comment 直写，绕过校验）。"""
        owner = self.user_id if user_id is None else user_id
        ids = [create_comment(slug, owner, "L1")]
        for level in range(2, length + 1):
            ids.append(create_comment(slug, owner, f"L{level}", parent_id=ids[-1]))
        return ids

    def _post(self, parent_id, content="x", **overrides):
        limits = dict(self.LIMITS)
        limits.update(overrides)
        return try_post_comment("post", self.user_id, "1.2.3.4", content, parent_id,
                                created_at="2026-01-01T00:00:00", max_depth=3,
                                **limits)

    def _count(self):
        with connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0]

    def test_core_rejects_reply_beyond_limit(self):
        """绕过路由直接调 core 也必须被拒（这是本次修复的核心）。"""
        ids = self._chain(3)
        before = self._count()
        self.assertEqual(self._post(ids[-1], "第 4 层"), "too_deep")
        self.assertEqual(self._count(), before, "超深层不应写入")

    def test_reject_does_not_write_rate_row(self):
        """被拒绝时不写限流流水（否则拒绝本身会放大表增长）。"""
        ids = self._chain(3)
        with connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM comment_rate").fetchone()[0]
        self.assertEqual(self._post(ids[-1]), "too_deep")
        with connect() as conn:
            after = conn.execute("SELECT COUNT(*) FROM comment_rate").fetchone()[0]
        self.assertEqual(after, before)

    def test_reply_within_limit_still_works(self):
        """不能误伤合法回复：回答第 2 层得到第 3 层，仍在限内。"""
        ids = self._chain(2)
        self.assertEqual(self._post(ids[-1], "第 3 层"), "ok")
        comments = get_comments_by_article("post")
        self.assertEqual(len(comments), 3)

    def test_top_level_comment_unaffected(self):
        """顶层评论（parent_id=None）不参与深度校验。"""
        self.assertEqual(self._post(None, "顶层"), "ok")

    def test_exactly_at_limit_is_rejected(self):
        """边界：父评论已经是第 N 层时，回复它就会到 N+1 -> 拒绝。"""
        ids = self._chain(3)                       # 最后一个 depth = 3 = max
        self.assertEqual(self._post(ids[1], "到第 3 层"), "ok")   # depth 2 -> 3 OK
        self.assertEqual(self._post(ids[-1], "到第 4 层"), "too_deep")

    def test_max_depth_zero_means_unlimited(self):
        """0 = 不限制（向后兼容旧调用，也是 config 允许的取值）。"""
        ids = self._chain(5)
        outcome = try_post_comment("post", self.user_id, "1.2.3.4", "无限制",
                                   ids[-1], created_at="2026-01-01T00:00:00",
                                   max_depth=0, **self.LIMITS)
        self.assertEqual(outcome, "ok")

    def test_depth_check_reuses_core_walk(self):
        """深度计算只有一处实现：core.db_comment.comment_depth。"""
        from elenvind.core.db_comment import comment_depth as core_depth
        from elenvind.modules.blog import logic
        ids = self._chain(3)
        row = get_comment_by_id(ids[-1])
        self.assertEqual(logic.comment_depth(row),
                         core_depth(row, max_depth=self._config["max_comment_depth"]))

    def test_concurrent_replies_cannot_exceed_limit(self):
        """并发回复同一个"已到顶"的父评论：全部被拒，链不会变深。"""
        import threading
        ids = self._chain(3)
        target = ids[-1]
        results = []
        lock = threading.Lock()

        def attempt(index):
            outcome = try_post_comment(
                "post", self.user_id, f"9.9.9.{index}", f"c{index}", target,
                created_at=f"2026-01-01T00:00:1{index}", max_depth=3,
                **self.LIMITS)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["too_deep"] * 4, results)
        deepest = max(logic_depth(get_comment_by_id(cid)) for cid in ids)
        self.assertLessEqual(deepest, 3)

    def test_too_deep_has_user_facing_message(self):
        from elenvind.modules.blog import logic
        ids = self._chain(3)
        outcome, message = logic.post_comment("post", self.user_id, "1.2.3.4",
                                              "hi", ids[-1])
        self.assertEqual(outcome, "too_deep")
        self.assertTrue(message)
        self.assertIn("depth", message.lower())

    def test_read_side_guard_untouched(self):
        """读侧的防环与 limit 兜底未被改动。"""
        from elenvind.core.db_comment import comment_depth as core_depth
        deep = self._chain(8)
        # limit 兜底：返回上限是 max_depth + 1
        self.assertEqual(logic_depth(get_comment_by_id(deep[-1])), 4)
        self.assertEqual(core_depth(get_comment_by_id(deep[-1]), max_depth=3), 4)

        # 成环：不死循环
        a = create_comment("post", self.user_id, "ring-a")
        b = create_comment("post", self.user_id, "ring-b", parent_id=a)
        with connect() as conn:
            conn.execute("UPDATE comment SET parent_id = ? WHERE id = ?", (b, a))
            conn.commit()
        self.assertLessEqual(logic_depth(get_comment_by_id(a)), 4)


class ReplyDepthHttpTests(ElenvindTestCase):
    """HTTP 层验收：直接 POST 深层 reply_to 被拒，且 comment 表无新行。"""

    def setUp(self):
        super().setUp()
        self._config["max_comment_depth"] = 2
        self.write_article("post", "body")
        self.create_user(nickname="Ann", email="ann@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com",
                                                "correct horse battery")
        self.cookies = self.app_cookies(session=self.session, csrf=self.csrf)

    def _post(self, reply_to):
        form = {"csrf_token": self.csrf, "content": "hello"}
        if reply_to is not None:
            form["reply_to"] = str(reply_to)
        return self.app.request("POST", "/article/post/comment", form=form,
                                cookies=self.cookies)

    def _count(self):
        with connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0]

    def test_post_beyond_depth_is_rejected_without_writing(self):
        root = create_comment("post", 1, "root")
        child = create_comment("post", 1, "child", parent_id=root)   # depth 2 = max
        before = self._count()
        response = self._post(child)          # 想写第 3 层
        self.assertEqual(response.status, 400)
        self.assertEqual(self._count(), before, "超深层不应写入")
        # 两条防线各有自己的文案：路由预检 "Reply depth limit reached"，
        # 事务内权威判定 "…maximum depth"。两条都是可读提示，任一条出现都算合格。
        body = response.text.lower()
        self.assertTrue("depth" in body or "maximum depth" in body, body[:200])

    def test_post_within_depth_succeeds(self):
        root = create_comment("post", 1, "root")
        response = self._post(root)           # 第 2 层，仍在限内
        self.assertEqual(response.status, 302)
        self.assertEqual(self._count(), 2)

    def test_top_level_post_succeeds(self):
        response = self._post(None)
        self.assertEqual(response.status, 302)

    def test_forged_deep_chain_from_client_is_rejected(self):
        """模拟攻击者用表单直连：链已到顶后反复提交，库里始终不增长。"""
        root = create_comment("post", 1, "root")
        child = create_comment("post", 1, "child", parent_id=root)
        before = self._count()
        for _ in range(3):
            self._post(child)
        self.assertEqual(self._count(), before)

    def test_rejection_message_is_actually_visible(self):
        """回归：被拒时页面必须显示提示，不能是"什么都没发生"。

        `_article_error()` 一直在传 message / message_kind，但 article.html
        曾经**没有渲染**它，于是所有评论拒绝（限流、非法 reply_to、层级到顶）
        都是静默失败：用户点提交，页面刷新一下，看不出哪里错了。
        """
        root = create_comment("post", 1, "root")
        child = create_comment("post", 1, "child", parent_id=root)
        response = self._post(child)
        self.assertEqual(response.status, 400)
        self.assertIn("form-msg", response.text)
        self.assertIn("error", response.text)
        self.assertIn("depth", response.text.lower())

    def test_rate_limit_rejection_is_visible_too(self):
        """同样的静默失败影响限流提示，一并锁住。"""
        self._config["comment_limits"] = {"max_per_user": 1, "max_per_ip": 100,
                                          "window_seconds": 60}
        first = self._post(None)
        self.assertEqual(first.status, 302)
        second = self._post(None)
        self.assertEqual(second.status, 429)
        self.assertIn("form-msg", second.text)
        self.assertIn("slow down", second.text)

    def test_invalid_reply_target_is_visible(self):
        response = self._post(999999)
        self.assertEqual(response.status, 400)
        self.assertIn("form-msg", response.text)
        self.assertIn("Invalid reply target", response.text)


def logic_depth(comment):
    """测试内取层级（走生产实现）。"""
    from elenvind.modules.blog import logic
    return logic.comment_depth(comment)


if __name__ == "__main__":
    unittest.main()
