"""comment 表：文章评论的读取、软删除/恢复与计数。

设计说明：
- 业务上的“删除”是**软删除**（is_deleted=1）：评论仍留在库里以便审计与恢复，
  页面展示层负责把已删评论对普通访客打码、对管理员划线显示。
  **没有物理删除入口**：评论一旦产生就永久保留（这也是审计前提）。
- `parent_id` 实现楼中楼回复；一次取回整篇文章评论后在内存里组树（树算法见
  features/blog/logic.py 的 build_comment_rows），避免 N+1 次查询。
  数据库层 `parent_id` 是 ON DELETE SET NULL，因此即便父行在库外被删除，
  子评论也会自动升级为顶层而不是消失。
- **本模块所有返回评论行的查询都带 `nickname` / `user_deleted`**（JOIN user），
  保证"单条"与"整篇"两种取法的行形状一致；调用方可以放心按同一套键读取。
- 评论的“写入 + 限流判定”在 db_comment_rate.try_post_comment 中原子完成。
"""
from datetime import datetime

from .db_base import connect


def get_comments_by_article(article_slug: str):
    """取回某文章全部评论（含软删除），按发表顺序排序。

    作者可能是已注销用户（user.is_deleted=1），展示层据此显示占位昵称。
    """
    with connect() as conn:
        return conn.execute("""
            SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at, c.parent_id,
                   c.is_deleted,
                   u.nickname, u.is_deleted AS user_deleted
            FROM comment c
            JOIN user u ON c.user_id = u.id
            WHERE c.article_slug = ?
            ORDER BY c.created_at ASC, c.id ASC
        """, (article_slug,)).fetchall()


def get_comment_by_id(comment_id: int):
    """按 id 取单条评论（含软删除），用于回复目标、删除/恢复前的归属与权限校验。

    **返回的行与 `get_comments_by_article()` 同形**（即带上 `nickname` /
    `user_deleted`）。这一点很重要：调用方既要用 `user_id` 判权限，
    也要用作者信息渲染"回复 @某某"提示，两处若形状不一致就会在补评论树时
    因缺列而 500（详见 tests/test_comments.py 的回归用例）。
    """
    with connect() as conn:
        return conn.execute("""
            SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at,
                   c.parent_id, c.is_deleted,
                   u.nickname, u.is_deleted AS user_deleted
            FROM comment c
            JOIN user u ON c.user_id = u.id
            WHERE c.id = ?
        """, (comment_id,)).fetchone()




def soft_delete_comment(comment_id: int):
    with connect() as conn:
        conn.execute("UPDATE comment SET is_deleted = 1 WHERE id = ?", (comment_id,))
        conn.commit()


def restore_comment(comment_id: int):
    with connect() as conn:
        conn.execute("UPDATE comment SET is_deleted = 0 WHERE id = ?", (comment_id,))
        conn.commit()


def create_comment(article_slug: str, user_id: int, content: str, parent_id: int = None) -> int:
    """写入一条评论，返回新 id（parent_id 为空表示顶层评论）。

    注意：Web 流程请用 db_comment_rate.try_post_comment（带原子限流）；
    本函数保留给脚本与测试使用。
    """
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (article_slug, user_id, content, datetime.now().isoformat(), parent_id)
        )
        conn.commit()
        return cursor.lastrowid
