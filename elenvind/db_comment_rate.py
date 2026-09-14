"""comment_rate 表：评论发布的短窗口限流计数（防刷屏）。

策略与 db_login 同款（逐条流水 + 窗口统计），维度为：
- 单用户窗口内评论数（正常读者连发很少超过阈值）；
- 单 IP 窗口内评论数（防同一出口批量刷屏）。
窗口都很短（60 秒），误伤概率低；超过 7 天的流水由启动清理任务删除。
"""
import time

from .db_base import get_connection


def record_comment_attempt(user_id: int, ip: str):
    """记录一次评论发布尝试（无论内容校验成败，只挡频率不挡内容）。"""
    conn = get_connection()
    # 顺带清理过期流水（低成本，不需要额外定时任务）
    conn.execute("DELETE FROM comment_rate WHERE attempted_at < ?", (time.time() - 7 * 86400,))
    conn.execute(
        "INSERT INTO comment_rate (user_id, ip, attempted_at) VALUES (?, ?, ?)",
        (user_id, ip, time.time())
    )
    conn.commit()
    conn.close()


def count_user_recent(user_id: int, window_seconds: int = 60) -> int:
    """统计窗口期内该用户的评论发布次数。"""
    conn = get_connection()
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM comment_rate WHERE user_id = ? AND attempted_at > ?",
        (user_id, cutoff)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def count_ip_recent(ip: str, window_seconds: int = 60) -> int:
    """统计窗口期内该 IP 的评论发布次数。"""
    conn = get_connection()
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM comment_rate WHERE ip = ? AND attempted_at > ?",
        (ip, cutoff)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def cleanup_old_comment_attempts(days: int = 7):
    """启动时删除指定天数之前的流水，控制表体积。"""
    conn = get_connection()
    conn.execute("DELETE FROM comment_rate WHERE attempted_at < ?", (time.time() - days * 86400,))
    conn.commit()
    conn.close()
