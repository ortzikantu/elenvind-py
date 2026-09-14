"""comment 表：文章评论的读写、软删除/恢复与物理清理。

设计说明：
- 业务上的“删除”是软删除（is_deleted=1）：评论仍留在库里以便审计/恢复，
  页面展示层负责把已删评论对普通访客打码、对管理员划线显示。
- parent_id 实现楼中楼回复；查询时一次取回整篇文章评论后在内存里组树，
  避免 N+1 次查询。
- 物理删除仅由管理脚本 purge.py 手工执行（先列出待删评论，确认后批量删除）。
"""
from datetime import datetime

from .db_base import get_connection


def get_comments_by_article(article_slug: str):
    """取回某文章全部评论（含软删除），并 JOIN 出作者与父评论作者信息。

    作者可能是已注销用户（user.is_deleted=1），展示层据此显示占位昵称。
    """
    conn = get_connection()
    comments = conn.execute("""
        SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at, c.parent_id,
               c.is_deleted,
               u.nickname, u.is_deleted AS user_deleted,
               p.nickname AS parent_nickname, p.id AS parent_user_id,
               p.is_deleted AS parent_user_deleted
        FROM comment c
        JOIN user u ON c.user_id = u.id
        LEFT JOIN comment pc ON c.parent_id = pc.id
        LEFT JOIN user p ON pc.user_id = p.id
        WHERE c.article_slug = ?
        ORDER BY c.created_at ASC, c.id ASC
    """, (article_slug,)).fetchall()
    conn.close()
    return comments


def create_comment(article_slug: str, user_id: int, content: str, parent_id: int = None) -> int:
    """写入一条评论，返回新 id（parent_id 为空表示顶层评论）。"""
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (article_slug, user_id, content, datetime.now().isoformat(), parent_id)
    )
    conn.commit()
    comment_id = cursor.lastrowid
    conn.close()
    return comment_id


def get_comment_by_id(comment_id: int):
    """按 id 取单条评论（含软删除），用于删除/恢复前的归属与权限校验。"""
    conn = get_connection()
    comment = conn.execute("SELECT * FROM comment WHERE id = ?", (comment_id,)).fetchone()
    conn.close()
    return comment


def soft_delete_comment(comment_id: int):
    conn = get_connection()
    conn.execute("UPDATE comment SET is_deleted = 1 WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()


def restore_comment(comment_id: int):
    conn = get_connection()
    conn.execute("UPDATE comment SET is_deleted = 0 WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()


def physical_delete_comments(comment_ids: list):
    """永久删除指定 ID 的评论（不级联子孙），仅供管理脚本使用。"""
    if not comment_ids:
        return
    conn = get_connection()
    # 动态 IN 子句：占位符数量与列表长度一致，值仍走参数绑定，无注入风险
    conn.execute("DELETE FROM comment WHERE id IN ({})".format(','.join('?' * len(comment_ids))), comment_ids)
    conn.commit()
    conn.close()


def get_all_soft_deleted_comments():
    """列出全部软删除评论（管理脚本用）。"""
    conn = get_connection()
    comments = conn.execute("""
        SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at,
               u.nickname
        FROM comment c
        JOIN user u ON c.user_id = u.id
        WHERE c.is_deleted = 1
        ORDER BY c.created_at ASC
    """).fetchall()
    conn.close()
    return comments
