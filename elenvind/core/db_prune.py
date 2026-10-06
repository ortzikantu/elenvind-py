"""限流流水表的**有界增长**：启动清理 + 低频机会式清理。

三个限流表（`login_attempts` / `register_attempts` / `comment_rate`）都是
"每次尝试写一行"，只在进程启动时清理会让长跑进程无界增长：

    一张每次登录失败写一行的表，在长期运行的站点上最终会有几百万行，
    而窗口查询虽然走索引，表文件与其 WAL 仍会持续膨胀。

但也不能把清理放进每次写入的事务里：`DELETE ... WHERE attempted_at < ?`
是 O(表大小) 的操作，放在写锁内会明显拉长锁持有时间（高并发互相排队）。
`db_register` 曾经就是每写一次清一次，与 `db_comment_rate` 刻意不清的做法
互相矛盾。

这里给出统一策略：**时间片**清理。只有当距上次清理超过 `interval_seconds`
时才真的执行 DELETE；调用点在写事务**之外**，因此不占写锁。

调用位置是硬约束：`prune()` 自己就是一个写事务（走 `write_tx()`，取同一把
flock），所以**绝不能**在另一个 `write_tx()` 块内调用它 —— 那会触发重入守卫
（否则就是自死锁）。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: 默认清理间隔：每小时一次。窗口最长不过几天，保留期以天计，
#: 因此这个频率足以把表压在几十万行以内（实际远小于此）。
DEFAULT_PRUNE_INTERVAL = 3600

_lock = threading.Lock()
#: {表名: 上次实际清理的时间戳}
_last_prune: dict[str, float] = {}


def due(table: str, interval_seconds: int = DEFAULT_PRUNE_INTERVAL) -> bool:
    """距上次清理是否已超过间隔；是则**占用**这一轮（避免并发重复清理）。"""
    now = time.time()
    with _lock:
        previous = _last_prune.get(table)
        if previous is not None and now - previous < interval_seconds:
            return False
        _last_prune[table] = now
        return True


def reset_state() -> None:
    """清空"上次清理时间"记录（测试用，避免用例之间互相影响）。"""
    with _lock:
        _last_prune.clear()


def prune(table: str, column: str, days: int,
          interval_seconds: int = DEFAULT_PRUNE_INTERVAL) -> int:
    """机会式清理：到期才删除 `column` 早于 `days` 天的行，返回删除行数。

    调用方必须在**写事务之外**调用它（例如写入提交之后），
    这样 DELETE 不会延长原事务的写锁持有时间。

    它自己是一个写事务（`write_tx()` → flock + BEGIN IMMEDIATE），因此
    **在另一个 write_tx() 块内调用会立刻触发重入守卫**。清理失败只记日志、
    不向上抛：这是"顺手的维护工作"，不该让业务写入失败（既有的容错语义）。
    """
    if not due(table, interval_seconds):
        return 0
    from .db_base import write_tx

    try:
        with write_tx() as conn:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {column} < ?",       # noqa: S608 - 表名/列名是代码内字面量
                (time.time() - days * 86400,))
            removed = cursor.rowcount or 0
    except Exception:                                             # noqa: BLE001
        logger.exception("Opportunistic prune of %s failed (ignored)", table)
        return 0
    if removed:
        logger.info("Pruned %s row(s) from %s", removed, table)
    return removed
