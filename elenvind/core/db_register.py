"""注册限流（register_attempts 表）：公开注册的 IP 维度限流。

与登录限流同款思路（逐条流水 + 窗口统计），但只按 IP 计数：
注册请求没有可信的账号维度，IP 是唯一可用信号。
默认窗口 1 小时、上限 5 次；个人站正常流量远低于此。
"""
import time

from .db_base import connect

RETENTION_DAYS = 7


def count_recent(ip: str, window_seconds: int) -> int:
    """统计窗口期内该 IP 的注册尝试次数。"""
    cutoff = time.time() - window_seconds
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM register_attempts WHERE ip = ? AND attempted_at > ?",
            (ip, cutoff)
        ).fetchone()
    return row[0] if row else 0


def try_register_attempt(ip: str, *, max_per_ip: int, window_seconds: int) -> bool:
    """原子判定并记账：允许则记录本次尝试并返回 True，超限返回 False。

    判定与记账放在同一个写事务里（BEGIN IMMEDIATE），避免并发请求
    各自读到旧计数后一起放行。
    """
    now = time.time()
    cutoff = now - window_seconds
    with connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM register_attempts WHERE attempted_at < ?",
                         (now - RETENTION_DAYS * 86400,))
            count = conn.execute(
                "SELECT COUNT(*) FROM register_attempts WHERE ip = ? AND attempted_at > ?",
                (ip, cutoff)
            ).fetchone()[0]
            if count >= max_per_ip:
                conn.rollback()
                return False
            conn.execute("INSERT INTO register_attempts (ip, attempted_at) VALUES (?, ?)",
                         (ip, now))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def cleanup_old_attempts(days: int = RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with connect() as conn:
        conn.execute("DELETE FROM register_attempts WHERE attempted_at < ?",
                     (time.time() - days * 86400,))
        conn.commit()
