"""评论"涂黑删除"与"编辑评论"的回归测试。

产品语义（本次改造）：

1. **删除 = 永久涂黑**：正文在数据库里被 U+2588 等长替换，原文不可恢复；
   评论行保留（`is_deleted = 1`）以维持 `parent_id` 树；
2. **取消恢复功能**：`/comment/restore/...` 不存在，任何角色都没有"撤销涂黑"入口；
3. **可编辑自己的评论**：Zero-JS 复用下面同一个输入框 —— 编辑按钮是
   `?edit=<id>#comment-form` 链接，服务端把 textarea 预填、action 指向编辑端点；
4. 权限：只有作者能改自己的话（管理员可以涂黑别人的话，但不能改写它）；
   已涂黑的评论不可编辑（原文已不存在）。
"""
import re
import sqlite3

import unittest

from tests.support import ElenvindTestCase

from elenvind import db
from elenvind.db import connection as db_connection
from elenvind.db.comment import (
    REDACTION_BLOCK,
    create_comment,
    get_comment_by_id,
    get_comments_by_article,
    redact_comment,
    update_comment_content,
)


class RedactionTests(ElenvindTestCase):
    """db 层：涂黑是不可逆的、写回数据库的。"""

    def setUp(self):
        super().setUp()
        self.user_id, _ = self.create_user(email="author@example.com")
        self.write_article("post", "body")

    def test_redaction_replaces_content_with_blocks_of_equal_length(self):
        comment_id = create_comment("post", self.user_id, "hello world")
        self.assertEqual(redact_comment(comment_id), 1)
        row = get_comment_by_id(comment_id)
        self.assertEqual(row["is_deleted"], 1)
        self.assertEqual(row["content"], REDACTION_BLOCK * len("hello world"))

    def test_original_text_is_gone_from_the_database_file(self):
        """端到端：原文不再以明文存在于库里（不是"渲染时才遮住"）。"""
        secret = "top-secret-value-42"
        comment_id = create_comment("post", self.user_id, secret)
        redact_comment(comment_id)

        raw = sqlite3.connect(str(db_connection.DB_PATH))
        try:
            blob = "".join(
                str(value) for row in raw.execute("SELECT * FROM comment")
                for value in row if value is not None)
        finally:
            raw.close()
        self.assertNotIn(secret, blob, "涂黑后库里仍能找到原文")

    def test_redaction_is_bounded(self):
        comment_id = create_comment("post", self.user_id, "x" * 5000)
        redact_comment(comment_id)
        row = get_comment_by_id(comment_id)
        self.assertEqual(len(row["content"]), db.REDACTION_MAX_BLOCKS)

    def test_redaction_keeps_the_row_and_the_tree(self):
        root = create_comment("post", self.user_id, "root")
        reply = create_comment("post", self.user_id, "reply", parent_id=root)
        redact_comment(root)
        self.assertIsNotNone(get_comment_by_id(root))
        self.assertEqual(get_comment_by_id(reply)["parent_id"], root)
        self.assertEqual(len(get_comments_by_article("post")), 2)

    def test_redaction_is_idempotent(self):
        comment_id = create_comment("post", self.user_id, "abcd")
        redact_comment(comment_id)
        first = get_comment_by_id(comment_id)["content"]
        redact_comment(comment_id)
        self.assertEqual(get_comment_by_id(comment_id)["content"], first)

    def test_unknown_comment_is_a_no_op(self):
        self.assertEqual(redact_comment(999999), 0)

    def test_update_refuses_redacted_comments(self):
        comment_id = create_comment("post", self.user_id, "abcd")
        self.assertEqual(update_comment_content(comment_id, "new"), 1)
        redact_comment(comment_id)
        self.assertEqual(update_comment_content(comment_id, "resurrect"), 0)
        self.assertEqual(set(get_comment_by_id(comment_id)["content"]), {REDACTION_BLOCK})


class RedactionMigrationTests(ElenvindTestCase):
    """v6 迁移：历史上"只标记不涂黑"的评论也被就地涂黑。"""

    def test_legacy_deleted_comments_are_redacted_on_startup(self):
        path = self.tmpdir / "legacy-v5.db"
        raw = sqlite3.connect(str(path))
        raw.executescript("""
            CREATE TABLE comment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                parent_id INTEGER,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted)
            VALUES ('post', 1, 'old secret', '2026-01-01', NULL, 1),
                   ('post', 1, 'visible',    '2026-01-01', NULL, 0);
            PRAGMA user_version = 5;
        """)
        raw.commit()
        raw.close()

        had = db_connection.DB_PATH
        db_connection.DB_PATH = path
        try:
            db.init_db()
            with db.connect() as conn:
                rows = {row["id"]: (row["content"], row["is_deleted"])
                        for row in conn.execute("SELECT id, content, is_deleted FROM comment")}
                version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            db_connection.DB_PATH = had

        self.assertEqual(version, 6)
        self.assertEqual(rows[1][0], REDACTION_BLOCK * len("old secret"))
        self.assertEqual(rows[1][1], 1)
        self.assertEqual(rows[2], ("visible", 0), "未删除的评论不能被改动")


class RedactionHttpTests(ElenvindTestCase):
    """HTTP 层：涂黑后的页面与"没有恢复入口"。"""

    def setUp(self):
        super().setUp()
        self.create_user(nickname="Ann", email="ann@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com", "correct horse battery")
        self.cookies = self.app_cookies(self.session, self.csrf)
        self.write_article("post", "body")
        response = self.app.request(
            "POST", "/article/post/comment",
            form={"csrf_token": self.csrf, "content": "original words"},
            cookies=self.cookies)
        self.assertEqual(response.status, 302)
        self.comment_id = get_comments_by_article("post")[0]["id"]

    def _delete(self, comment_id=None):
        return self.app.request(
            "POST", f"/article/post/comment/delete/{comment_id or self.comment_id}",
            form={"csrf_token": self.csrf}, cookies=self.cookies)

    def test_delete_redacts_and_the_page_shows_blocks(self):
        self.assertEqual(self._delete().status, 302)
        page = self.app.request("GET", "/article/post", cookies=self.cookies).text
        self.assertNotIn("original words", page)
        self.assertIn("is-redacted", page)
        self.assertIn(REDACTION_BLOCK, page)
        # 提示语固定为"数据删除"，且必须紧跟黑块之后（在正文里，不在 meta 行）
        self.assertIn("Data deleted", page)
        self.assertLess(page.index(REDACTION_BLOCK), page.index("Data deleted"),
                        "提示语应加在黑块后面")

    def test_no_restore_entry_anywhere(self):
        self._delete()
        page = self.app.request("GET", "/article/post", cookies=self.cookies).text
        self.assertNotIn("/comment/restore/", page)
        self.assertNotIn("Restore", page)
        for method in ("GET", "POST"):
            response = self.app.request(
                method, f"/article/post/comment/restore/{self.comment_id}",
                form={"csrf_token": self.csrf}, cookies=self.cookies)
            self.assertIn(response.status, (404, 405), response.status)

    def test_redacted_comment_is_irreversible(self):
        self._delete()
        row = get_comment_by_id(self.comment_id)
        self.assertEqual(row["is_deleted"], 1)
        self.assertEqual(set(row["content"]), {REDACTION_BLOCK})
        self.assertIsNone(db.update_comment_content(self.comment_id, "bring it back") or None)

    def test_cannot_reply_form_still_offers_reply_on_redacted_comment(self):
        """涂黑后仍可回复（树结构与讨论都还在），只是正文没了。"""
        self._delete()
        response = self.app.request(
            "POST", "/article/post/comment",
            form={"csrf_token": self.csrf, "content": "reply", "reply_to": str(self.comment_id)},
            cookies=self.cookies)
        self.assertEqual(response.status, 302)
        self.assertEqual(len(get_comments_by_article("post")), 2)


class CommentEditHttpTests(ElenvindTestCase):
    """HTTP 层：编辑自己的评论（复用同一个输入框）。"""

    def setUp(self):
        super().setUp()
        self.ann_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.bob_id, _ = self.create_user(nickname="Bob", email="bob@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com", "correct horse battery")
        self.cookies = self.app_cookies(self.session, self.csrf)
        self.write_article("post", "body")
        self.comment_id = create_comment("post", self.ann_id, "first version")

    def _edit(self, content, comment_id=None, **kwargs):
        return self.app.request(
            "POST", f"/article/post/comment/edit/{comment_id or self.comment_id}",
            form={"csrf_token": kwargs.get("csrf", self.csrf), "content": content},
            cookies=kwargs.get("cookies", self.cookies))

    def test_author_can_edit_own_comment(self):
        response = self._edit("second version")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.header("location"), "/article/post#comments")
        self.assertEqual(get_comment_by_id(self.comment_id)["content"], "second version")
        self.assertIn("second version",
                      self.app.request("GET", "/article/post", cookies=self.cookies).text)

    def test_edit_form_is_the_same_input_box_prefilled(self):
        """`?edit=<id>` 让下面那个输入框变成"编辑模式"：预填 + 指向编辑端点。"""
        page = self.app.request("GET", "/article/post",
                                query={"edit": str(self.comment_id)},
                                cookies=self.cookies).text
        self.assertIn(f'action="/article/post/comment/edit/{self.comment_id}"', page)
        self.assertIn("first version", page, "textarea 应预填待编辑的正文")
        self.assertIn("Edit this comment", page)
        self.assertIn("comment-compose is-editing", page)
        self.assertIn("Cancel", page)

    def test_plain_form_is_not_in_edit_mode(self):
        page = self.app.request("GET", "/article/post", cookies=self.cookies).text
        self.assertIn('action="/article/post/comment"', page)
        self.assertNotIn("is-editing", page)
        self.assertNotIn("first version</textarea>", page.replace("\n", ""))

    def test_cannot_edit_someone_elses_comment(self):
        other = create_comment("post", self.bob_id, "bob's words")
        response = self._edit("hijacked", comment_id=other)
        self.assertEqual(response.status, 403)
        self.assertEqual(get_comment_by_id(other)["content"], "bob's words")

    def test_admin_cannot_rewrite_other_peoples_words(self):
        """管理员可以涂黑别人的话（审核），但不能改写它（伪造发言）。"""
        other = create_comment("post", self.bob_id, "bob's words")
        admin_session, admin_csrf = self.login_ok("ann@example.com", "correct horse battery")
        # 让 ann 变成管理员，但评论仍然是 bob 的
        self._config["admin_user_id"] = self.ann_id
        response = self._edit("rewritten by admin", comment_id=other,
                              csrf=admin_csrf,
                              cookies=self.app_cookies(admin_session, admin_csrf))
        self.assertEqual(response.status, 403)
        self.assertEqual(get_comment_by_id(other)["content"], "bob's words")

    def test_admin_can_still_redact_other_peoples_comments(self):
        other = create_comment("post", self.bob_id, "bob's words")
        self._config["admin_user_id"] = self.ann_id
        response = self.app.request(
            "POST", f"/article/post/comment/delete/{other}",
            form={"csrf_token": self.csrf}, cookies=self.cookies)
        self.assertEqual(response.status, 302)
        self.assertEqual(get_comment_by_id(other)["is_deleted"], 1)

    def test_redacted_comment_cannot_be_edited(self):
        redact_comment(self.comment_id)
        response = self._edit("resurrect attempt")
        self.assertEqual(response.status, 400)
        self.assertEqual(set(get_comment_by_id(self.comment_id)["content"]),
                         {REDACTION_BLOCK})

    def test_edit_view_for_redacted_comment_is_not_prefilled(self):
        redact_comment(self.comment_id)
        page = self.app.request("GET", "/article/post",
                                query={"edit": str(self.comment_id)},
                                cookies=self.cookies).text
        self.assertIn('action="/article/post/comment"', page)
        self.assertNotIn("is-editing", page)

    def test_empty_and_too_long_edits_are_rejected(self):
        for content in ("", "   ", "x" * (db_comment_max(self) + 1)):
            with self.subTest(length=len(content)):
                response = self._edit(content)
                self.assertEqual(response.status, 400)
                self.assertEqual(get_comment_by_id(self.comment_id)["content"],
                                 "first version")

    def test_edit_requires_csrf(self):
        response = self._edit("no token", csrf="x" * 43)
        self.assertEqual(response.status, 400)
        self.assertEqual(get_comment_by_id(self.comment_id)["content"], "first version")

    def test_edit_requires_login(self):
        response = self.app.request(
            "POST", f"/article/post/comment/edit/{self.comment_id}",
            form={"csrf_token": self.csrf, "content": "anon"})
        # 未登录：认证闸门重定向到 /login，或 CSRF 闸门直接 400 —— 都不能改内容
        self.assertIn(response.status, (302, 400, 403))
        self.assertEqual(get_comment_by_id(self.comment_id)["content"], "first version")

    def test_editing_does_not_move_the_comment_in_the_tree(self):
        root = create_comment("post", self.ann_id, "root")
        reply = create_comment("post", self.ann_id, "child", parent_id=root)
        self.assertEqual(self._edit("child edited", comment_id=reply).status, 302)
        self.assertEqual(get_comment_by_id(reply)["parent_id"], root)
        page = self.app.request("GET", "/article/post", cookies=self.cookies).text
        self.assertIn("child edited", page)
        self.assertLess(page.index("root"), page.index("child edited"))

    def test_editing_one_comment_leaves_the_others_alone(self):
        second = create_comment("post", self.ann_id, "second comment")
        self.assertEqual(self._edit("only me").status, 302)
        self.assertEqual(get_comment_by_id(second)["content"], "second comment")

    def test_unknown_comment_id_is_not_a_500(self):
        for raw in ("abc", "-1", "99999999", "1e5"):
            with self.subTest(raw=raw):
                response = self._edit("x", comment_id=raw)
                self.assertIn(response.status, (400, 403, 404))
                self.assertNotEqual(response.status, 500)

    def test_edit_comment_of_another_article_is_rejected(self):
        self.write_article("other", "body")
        response = self.app.request(
            "POST", f"/article/other/comment/edit/{self.comment_id}",
            form={"csrf_token": self.csrf, "content": "cross article"},
            cookies=self.cookies)
        self.assertEqual(response.status, 403)
        self.assertEqual(get_comment_by_id(self.comment_id)["content"], "first version")


def db_comment_max(self):
    """当前配置的评论长度上限（避免把 1000 写死在测试里）。"""
    from elenvind.modules.blog import logic
    return logic.default_max_length()


if __name__ == "__main__":
    unittest.main()


class CommentPresentationTests(ElenvindTestCase):
    """展示层契约：不做头像、时间可读、"数据删除"只出现在涂黑评论上。"""

    def setUp(self):
        super().setUp()
        self.user_id, _ = self.create_user(nickname="Ann", email="ann@example.com")
        self.session, self.csrf = self.login_ok("ann@example.com", "correct horse battery")
        self.cookies = self.app_cookies(self.session, self.csrf)
        self.write_article("post", "body")
        self.comment_id = create_comment("post", self.user_id, "hello there")

    def _page(self):
        return self.app.request("GET", "/article/post", cookies=self.cookies).text

    def test_no_avatar_markup(self):
        """本项目没有设计头像功能：页面上不得出现头像容器/占位字符。"""
        page = self._page()
        self.assertNotIn("comment-avatar", page)
        self.assertNotIn("avatar", page.lower())

    def test_comment_time_is_human_readable(self):
        """库里存 ISO，界面必须显示成 YYYY-MM-DD HH:MM（不能带 T / 微秒）。"""
        page = self._page()
        stamp = re.search(r'<span class="comment-time">([^<]*)</span>', page)
        self.assertIsNotNone(stamp, "评论时间容器缺失")
        value = stamp.group(1)
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", value)
        self.assertNotIn("T", value)
        self.assertNotIn(".", value)

    def test_rows_carry_the_formatted_time(self):
        from elenvind.modules.blog import logic
        rows, _, _ = logic.build_comment_rows("post", None, max_length=1000)
        self.assertRegex(rows[0]["created"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_deletion_tag_only_on_redacted_comments(self):
        self.assertNotIn("Data deleted", self._page())
        redact_comment(self.comment_id)
        page = self._page()
        self.assertIn("Data deleted", page)
        self.assertLess(page.index(REDACTION_BLOCK), page.index("Data deleted"))

    def test_redaction_tag_is_inside_the_body_after_the_blocks(self):
        redact_comment(self.comment_id)
        page = self._page()
        body = page[page.index('class="comment-body"'):]
        self.assertIn("comment-redacted-tag", body)
        self.assertLess(body.index(REDACTION_BLOCK), body.index("comment-redacted-tag"))
