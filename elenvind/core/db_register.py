"""注册限流（register_attempts 表）：公开注册的 IP 维度限流。

与登录限流同款思路（逐条流水 + 窗口统计），但只按 IP 计数：
注册请求没有可信的账号维度，IP 是唯一可用信号。
默认窗口 1 小时、上限 5 次；个人站正常流量远低于此。

流水清理分两层（见 `core.db_prune`）：启动时全量清一次，
运行期在提交之后机会式清理（每小时最多一次）。
**刻意不放在写事务里**：那是 O(表大小) 的 DELETE，会拉长写锁持有时间
（`db_comment_rate` 早先也是这个策略，两者现已统一）；而且嵌套
`write_tx()` 会触发重入守卫，属于设计上不允许的用法。

写入统一走 `write_tx()`（跨进程 flock + BEGIN IMMEDIATE）。
"""
import time

from .db_base import write_tx
from .db_prune import prune

RETENTION_DAYS = 7




def try_register_attempt(ip: str, *, max_per_ip: int, window_seconds: int) -> bool:
    """原子判定并记账：允许则记录本次尝试并返回 True，超限返回 False。

    判定与记账放在**同一个** `write_tx()` 里（跨进程 flock + BEGIN IMMEDIATE），
    避免并发请求各自读到旧计数后一起放行。超限时显式 `rollback()`（其实没写过
    任何行），由 write_tx 的收尾保持"什么都没发生"的语义。
    """
    now = time.time()
    cutoff = now - window_seconds
    with write_tx() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM register_attempts WHERE ip = ? AND attempted_at > ?",
            (ip, cutoff)
        ).fetchone()[0]
        if count >= max_per_ip:
            conn.rollback()
            return False
        conn.execute("INSERT INTO register_attempts (ip, attempted_at) VALUES (?, ?)",
                     (ip, now))
    # 写事务结束（锁已释放）之后才做机会式清理，避免 DELETE 拉长写锁持有时间；
    # 也避免在 write_tx 里再取一次锁（嵌套会触发重入守卫）。
    prune("register_attempts", "attempted_at", RETENTION_DAYS)
    return True


def cleanup_old_attempts(days: int = RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM register_attempts WHERE attempted_at < ?",
                     (time.time() - days * 86400,))
