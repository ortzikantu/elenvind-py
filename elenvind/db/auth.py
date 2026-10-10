"""认证相关持久化：登录失败流水与注册尝试流水（数据领域 = auth attempts）。

数据对象：
- `login_attempts(email, ip, attempted_at, success)`：登录失败/成功流水，
  用于 email / ip / global 三个维度的窗口计数；
- `register_attempts(ip, attempted_at)`：注册尝试流水，用于注册 IP 限流。

这里只负责"怎么可靠地读写这些行"，"什么时候该拒绝一次登录/注册"由
`modules/auth` 决定（限流判定与记账的原子性由本层的 write_tx() 保证）。
"""
from __future__ import annotations

import logging
import time

from .maintenance import prune
from .transaction import write_tx

logger = logging.getLogger(__name__)


#: 登录流水保留期（天）。启动清理与运行期机会式清理共用。
RETENTION_DAYS = 30
#: 注册尝试流水的保留期（天）。与登录流水**分开**命名：注册限流窗口只有
#: 一小时，留 7 天足够排查。历史上两个用途共用一个模块级 `RETENTION_DAYS`，
#: 且第 209 行又把 30 就地覆盖成 7 —— 结果是"运行期清理按 7 天、启动清理按
#: 30 天"的隐形不一致（同名常量有两个值）。
REGISTER_RETENTION_DAYS = 7


def record_login_attempt(email: str, ip: str, success: bool):
    """记录一次登录尝试（成败都记，成功记录用于审计）。

    提交后做一次机会式清理（每小时最多一次，见 `db.maintenance`）：
    登录失败是攻击者最容易制造的行增长来源，只靠启动清理在长跑进程上
    会让表无限膨胀。

    `prune()` 必须在 `write_tx()` 块**之外**调用：它自己也要写（取同一把
    flock），嵌套会立刻触发重入守卫（否则就是自死锁）。
    """
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO login_attempts (email, ip, attempted_at, success) VALUES (?, ?, ?, ?)",
            (email, ip, time.time(), 1 if success else 0)
        )
    prune("login_attempts", "attempted_at", RETENTION_DAYS)


#: 渐进 backoff 的默认参数（模块层可通过参数覆盖）
COOLDOWN_BASE_SECONDS = 60
COOLDOWN_MAX_SECONDS = 24 * 3600


#: 允许的计数维度 -> (SQL 片段, 参数个数)。SQL 片段**只来自这张表**，
#: 调用方只能传维度名（防"把外部字符串拼进 SQL"的标识符注入面）。
_COUNT_SCOPES = {
    "any": ("1 = 1", 0),
    "email": ("email = ? COLLATE NOCASE", 1),
    "ip": ("ip = ?", 1),
}


def _count_and_last(conn, scope: str, params, cutoff: float):
    """窗口内失败次数 + 最近一次失败时间（同一条查询，避免两次往返）。"""
    clause, arity = _COUNT_SCOPES[scope]           # 未知维度直接 KeyError（失效关闭）
    if len(params) != arity:
        raise ValueError(f"scope {scope!r} 需要 {arity} 个参数")
    row = conn.execute(
        "SELECT COUNT(*) AS cnt, MAX(attempted_at) AS last FROM login_attempts "
        f"WHERE success = 0 AND attempted_at > ? AND {clause}",
        (cutoff, *params),
    ).fetchone()
    return (row["cnt"] or 0), row["last"]


def _cooldown_seconds(count: int, limit: int, base: int, maximum: int) -> int:
    """达到阈值后的冷却时长：按超出次数指数增长，封顶 `maximum`。

    第 `limit` 次失败 -> base；之后每多一次失败翻倍。
    这样"偶尔打错的正常用户"只需要等约 1 分钟，
    而持续爆破的攻击者等待时间迅速增长到封顶（不再是一刀切 24h）。
    """
    over = max(count - limit, 0)
    return int(min(base * (2 ** min(over, 20)), maximum))


def reserve_login_attempt(email: str, ip: str, *,
                          max_email_failures: int, email_window_seconds: int,
                          max_ip_failures: int, ip_window_seconds: int,
                          max_global_failures: int, global_window_seconds: int,
                          cooldown_base_seconds: int = COOLDOWN_BASE_SECONDS,
                          cooldown_max_seconds: int = COOLDOWN_MAX_SECONDS,
                          attempted_at: float | None = None):
    """**一个写事务**里完成三闸门判定 + 占位记账。

    返回 `(allowed: bool, reason: str, retry_after: int)`：

    - `allowed=True`：已经写入一行 `success = 0` 的**占位**（代表这次尝试），
      调用方随后做 scrypt 校验。校验成功要调用 `complete_login_success()`
      把该邮箱的失败流水清掉；失败则什么都不用做（占位就是这次失败）。
    - `allowed=False`：**不写任何行**，`reason` ∈ {"global","email","ip"}，
      `retry_after` 是建议等待秒数（渐进 backoff 的结果）。

    为什么把"判定 + 记账"合成一个事务：旧实现是"读计数 → 校验 → 记账"，
    中间夹着约 240ms 的 scrypt，并发请求会同时读到低于阈值而全部放行。
    现在判定与写入在同一个 `BEGIN IMMEDIATE` 里，无法超发。
    """
    now = time.time() if attempted_at is None else attempted_at
    with write_tx() as conn:
        global_count, global_last = _count_and_last(
            conn, "any", (), now - global_window_seconds)
        email_count, email_last = _count_and_last(
            conn, "email", (email,), now - email_window_seconds)
        ip_count, ip_last = _count_and_last(
            conn, "ip", (ip,), now - ip_window_seconds)

        # 顺序与原实现一致：global 是分布式爆破的最后闸门，先判它
        for reason, count, limit, last in (
                ("global", global_count, max_global_failures, global_last),
                ("email", email_count, max_email_failures, email_last),
                ("ip", ip_count, max_ip_failures, ip_last)):
            if count < limit:
                continue
            cooldown = _cooldown_seconds(count, limit, cooldown_base_seconds,
                                         cooldown_max_seconds)
            elapsed = now - (last or now)
            if elapsed < cooldown:
                return False, reason, max(int(cooldown - elapsed), 1)
            # 冷窗已过：允许这次尝试（计数仍会继续增长，冷却随时长自然重置）

        conn.execute(
            "INSERT INTO login_attempts (email, ip, attempted_at, success) "
            "VALUES (?, ?, ?, 0)",
            (email, ip, now)
        )
    # prune() 自己是一个写事务，必须在上面的事务之外调用
    prune("login_attempts", "attempted_at", RETENTION_DAYS)
    return True, "ok", 0


def complete_login_success(email: str, ip: str, *, user_id: int | None = None,
                           new_password_hash: str | None = None,
                           attempted_at: float | None = None):
    """登录成功后的收尾（**一个写事务**）：清失败流水 + 记成功 + 可选 rehash。

    三件事同一事务：不会出现"失败流水清了一半 / rehash 落了但审计没落"。
    """
    now = time.time() if attempted_at is None else attempted_at
    with write_tx() as conn:
        conn.execute("DELETE FROM login_attempts WHERE email = ? COLLATE NOCASE",
                     (email,))
        conn.execute(
            "INSERT INTO login_attempts (email, ip, attempted_at, success) VALUES (?, ?, ?, 1)",
            (email, ip, now)
        )
        if user_id is not None and new_password_hash:
            conn.execute("UPDATE user SET password = ? WHERE id = ?",
                         (new_password_hash, user_id))
    prune("login_attempts", "attempted_at", RETENTION_DAYS)


def clear_login_attempts(email: str):
    """用户登录成功后清空其失败流水（大小写不敏感），避免旧失败继续锁号。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM login_attempts WHERE email = ? COLLATE NOCASE", (email,))


def cleanup_old_login_attempts(days: int = RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with write_tx() as conn:
        cutoff = time.time() - days * 86400
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))


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
    prune("register_attempts", "attempted_at", REGISTER_RETENTION_DAYS)
    return True


def cleanup_old_attempts(days: int = REGISTER_RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM register_attempts WHERE attempted_at < ?",
                     (time.time() - days * 86400,))
