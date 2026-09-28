"""评论发布限流（comment_rate 表）：短窗口计数，防刷屏。

策略与 db_login 同款（逐条流水 + 窗口统计），维度为：
- 单用户窗口内评论数（正常读者连发很少超过阈值）；
- 单 IP 窗口内评论数（防同一出口批量刷屏）。
窗口都很短（默认 60 秒），误伤概率低；超过 7 天的流水由启动清理任务删除。

并发说明：限流判定与评论写入必须在一个事务里完成才真正原子，
因此对外主入口是 try_post_comment() —— 由它统一取号、计数、插入。
"""
import time
from contextlib import closing

from .db_base import get_connection

RETENTION_DAYS = 7


def cleanup_old_comment_attempts(days: int = RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM comment_rate WHERE attempted_at < ?",
                     (time.time() - days * 86400,))
        conn.commit()


def try_post_comment(article_slug: str, user_id: int, ip: str, content: str,
                     parent_id=None, *, max_per_user: int, max_per_ip: int,
                     window_seconds: int, max_per_article: int,
                     created_at: str) -> str:
    """原子地判定限流并写入评论。

    返回 "ok" / "rate_user" / "rate_ip" / "too_many"；
    限流命中时不写流水、不写评论（避免把攻击流量放大成表增长）。
    """
    now = time.time()
    cutoff = now - window_seconds
    with closing(get_connection()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")   # 立刻取写锁：计数与插入之间不被并发插入插队
            conn.execute("DELETE FROM comment_rate WHERE attempted_at < ?",
                         (now - RETENTION_DAYS * 86400,))
            user_count = conn.execute(
                "SELECT COUNT(*) FROM comment_rate WHERE user_id = ? AND attempted_at > ?",
                (user_id, cutoff)).fetchone()[0]
            if user_count >= max_per_user:
                conn.rollback()
                return "rate_user"
            ip_count = conn.execute(
                "SELECT COUNT(*) FROM comment_rate WHERE ip = ? AND attempted_at > ?",
                (ip, cutoff)).fetchone()[0]
            if ip_count >= max_per_ip:
                conn.rollback()
                return "rate_ip"
            article_count = conn.execute(
                "SELECT COUNT(*) FROM comment WHERE article_slug = ?", (article_slug,)
            ).fetchone()[0]
            if article_count >= max_per_article:
                conn.rollback()
                return "too_many"
            conn.execute(
                "INSERT INTO comment_rate (user_id, ip, attempted_at) VALUES (?, ?, ?)",
                (user_id, ip, now))
            conn.execute(
                "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (article_slug, user_id, content, created_at, parent_id))
            conn.commit()
            return "ok"
        except Exception:
            conn.rollback()
            raise
