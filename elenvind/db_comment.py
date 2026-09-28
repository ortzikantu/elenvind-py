"""comment 表：文章评论的读取、软删除/恢复、计数与物理清理。

设计说明：
- 业务上的“删除”是软删除（is_deleted=1）：评论仍留在库里以便审计/恢复，
  页面展示层负责把已删评论对普通访客打码、对管理员划线显示。
- parent_id 实现楼中楼回复；一次取回整篇文章评论后在内存里组树（树算法见
  view_partials_comment.build_tree），避免 N+1 次查询。
- 物理删除仅由管理脚本 purge.py 手工执行（先列出待删评论，确认后批量删除）；
  数据库层 parent_id 是 ON DELETE SET NULL，父行被物理删除时子评论自动升级为顶层。
- 评论的“写入 + 限流判定”在 db_comment_rate.try_post_comment 中原子完成。
"""
from contextlib import closing
from datetime import datetime

from .db_base import get_connection


def get_comments_by_article(article_slug: str):
    """取回某文章全部评论（含软删除），按发表顺序排序。

    作者可能是已注销用户（user.is_deleted=1），展示层据此显示占位昵称。
    """
    with closing(get_connection()) as conn:
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
    """按 id 取单条评论（含软删除），用于删除/恢复前的归属与权限校验。"""
    with closing(get_connection()) as conn:
        return conn.execute("SELECT * FROM comment WHERE id = ?", (comment_id,)).fetchone()


def count_by_article(article_slug: str) -> int:
    """该文章的评论总数（含软删除），用于“单篇文章评论上限”判定。"""
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM comment WHERE article_slug = ?", (article_slug,)
        ).fetchone()
    return row[0] if row else 0


def soft_delete_comment(comment_id: int):
    with closing(get_connection()) as conn:
        conn.execute("UPDATE comment SET is_deleted = 1 WHERE id = ?", (comment_id,))
        conn.commit()


def restore_comment(comment_id: int):
    with closing(get_connection()) as conn:
        conn.execute("UPDATE comment SET is_deleted = 0 WHERE id = ?", (comment_id,))
        conn.commit()


def create_comment(article_slug: str, user_id: int, content: str, parent_id: int = None) -> int:
    """写入一条评论，返回新 id（parent_id 为空表示顶层评论）。

    注意：Web 流程请用 db_comment_rate.try_post_comment（带原子限流）；
    本函数保留给脚本与测试使用。
    """
    with closing(get_connection()) as conn:
        cursor = conn.execute(
            "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (article_slug, user_id, content, datetime.now().isoformat(), parent_id)
        )
        conn.commit()
        return cursor.lastrowid


def physical_delete_comments(comment_ids: list):
    """永久删除指定 ID 的评论（不级联子孙），仅供管理脚本使用。"""
    if not comment_ids:
        return
    with closing(get_connection()) as conn:
        # 动态 IN 子句：占位符数量与列表长度一致，值仍走参数绑定，无注入风险
        conn.execute("DELETE FROM comment WHERE id IN ({})".format(','.join('?' * len(comment_ids))),
                     comment_ids)
        conn.commit()


def get_all_soft_deleted_comments():
    """列出全部软删除评论（管理脚本用）。"""
    with closing(get_connection()) as conn:
        return conn.execute("""
            SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at,
                   u.nickname
            FROM comment c
            JOIN user u ON c.user_id = u.id
            WHERE c.is_deleted = 1
            ORDER BY c.created_at ASC
        """).fetchall()
