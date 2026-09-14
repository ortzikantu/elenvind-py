"""login_attempts 表：登录失败的记录与限流查询。

策略说明：
- 逐条记录失败流水而不是维护计数器，便于按“邮箱 / IP / 全局”三个维度分别统计。
- 邮箱维度是主防线：24h 窗口内失败次数超限即锁该账号（防定向爆破）；
  匹配使用 COLLATE NOCASE，防止攻击者变换大小写绕过。
- IP 维度是辅助防线：只统计短窗口（15 分钟），阈值放宽到 20 次——
  避免 NAT / 公司出口共享同一 IP 的普通用户被他人连累而长时间误锁。
- 流水只在窗口期内生效，超过 30 天的记录由启动时的清理任务删除。
"""
import time

from .db_base import get_connection


def record_login_attempt(email: str, ip: str, success: bool):
    """记录一次登录尝试（成败都记，成功记录用于审计）。"""
    conn = get_connection()
    conn.execute(
        "INSERT INTO login_attempts (email, ip, attempted_at, success) VALUES (?, ?, ?, ?)",
        (email, ip, time.time(), 1 if success else 0)
    )
    conn.commit()
    conn.close()


def count_email_failures(email: str, window_seconds: int = 900) -> int:
    """统计窗口期内指定邮箱（大小写不敏感）的失败次数。"""
    conn = get_connection()
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts WHERE success = 0 AND attempted_at > ? "
        "AND email = ? COLLATE NOCASE",
        (cutoff, email)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def count_ip_failures(ip: str, window_seconds: int = 900) -> int:
    """统计窗口期内指定 IP 的失败次数。"""
    conn = get_connection()
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts WHERE success = 0 AND attempted_at > ? "
        "AND ip = ?",
        (cutoff, ip)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def count_global_recent_failures(window_seconds: int = 900) -> int:
    """统计窗口期内全站失败次数：分布式（多 IP）爆破的最后一道闸门。"""
    conn = get_connection()
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts WHERE success = 0 AND attempted_at > ?",
        (cutoff,)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def clear_login_attempts(email: str):
    """用户登录成功后清空其失败流水（大小写不敏感），避免旧失败继续锁号。"""
    conn = get_connection()
    conn.execute("DELETE FROM login_attempts WHERE email = ? COLLATE NOCASE", (email,))
    conn.commit()
    conn.close()


def cleanup_old_login_attempts(days: int = 30):
    """启动时删除指定天数之前的流水，控制表体积。"""
    conn = get_connection()
    cutoff = time.time() - days * 86400
    conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
    conn.commit()
    conn.close()
