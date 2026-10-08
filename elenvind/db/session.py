"""session 表：服务端会话的存取（登录发证 / 鉴权查证 / 注销删除）。

安全说明：
- token 是 32 字节 cryptographically-secure 随机串，仅存在于服务端表与用户 Cookie 中，
  攻击者无法预测，也不像 JWT 那样携带可伪造的载荷。
- 登录成功会先 delete_user_sessions(user_id) 再发新证，防止会话固定攻击。
- 每次发证与每次校验都顺带清理过期会话，避免长运行后表无限增长。
- 本模块**全是写操作**（含读-判断-写复合操作），因此统一走 `write_tx()`：
  flock 保证跨 worker 互斥，BEGIN IMMEDIATE 保证事务原子性。

过期策略（两个维度，都需要满足才有效）：
- **绝对过期** `created_at + absolute_days`：会话无论多活跃，活够这么久就必须重新登录。
  这是"token 泄露后风险窗口"的硬上限。
- **滑动过期** `last_seen + idle_days`：闲置超过这么久即失效。
  每次有效访问会刷新 `last_seen`，所以常用设备不会被踢。
两者任一超时都视为无效，并**当场删除**该行（不给重复利用留机会）。
阈值来自 config.toml（`session_absolute_days` / `session_idle_days`），
设为 0 表示该维度不过期——那会显著放大 token 泄露的风险，站长需自行权衡。
"""
import secrets
import time

from .transaction import write_tx

#: 兜底默认值（配置缺失时使用）。config.toml 里的同名键是实际生效来源。
DEFAULT_ABSOLUTE_DAYS = 30
DEFAULT_IDLE_DAYS = 15

DAY_SECONDS = 86400

#: 生效窗口（由装配层按 config.toml 注入；db 不读配置）
_LIMITS = {"absolute_days": DEFAULT_ABSOLUTE_DAYS, "idle_days": DEFAULT_IDLE_DAYS}


#: 装配层注入的窗口来源（`() -> (absolute_days, idle_days)`）。
#: db 不认识 config，所以这里只保存一个 callable；调用时**实时**取值，
#: 因此运行期改配置（测试里就是这么做的）会立刻生效。
_limits_provider = None


def set_limits_provider(provider) -> None:
    """装配层注入会话过期窗口来源（`elenvind/app.py` 调用）。"""
    global _limits_provider
    _limits_provider = provider


def _session_limits():
    """返回 (absolute_days, idle_days)；0 表示该维度不设过期。

    数值来自装配层注入的 provider（config 层已校验类型/范围）；
    未注入时用兜底默认值。这里再做一次防御性 `int()`：配置层若被绕过，
    也不能让非数值混进比较运算。
    """
    if _limits_provider is not None:
        try:
            absolute, idle = _limits_provider()
            return max(int(absolute), 0), max(int(idle), 0)
        except (TypeError, ValueError):
            pass
    return _LIMITS["absolute_days"], _LIMITS["idle_days"]

def _is_expired(row, absolute_days: int, idle_days: int, now: float) -> bool:
    """判断会话行是否已过期（两个维度任一命中）。

    **失败关闭**：时间戳只要不是可用数值（NULL、字符串、NaN）就视为已过期。
    SQLite 是动态类型，声明为 REAL 的列其实可以存进 TEXT；旧实现只判了 NULL，
    遇到字符串会直接 `TypeError` 冒到 500 —— 那样每个带该 Cookie 的请求都 500，
    而不是干脆当作未登录（用户重新登录即可自愈）。
    """
    created_at = _as_timestamp(row["created_at"])
    last_seen = _as_timestamp(row["last_seen"])
    if created_at is None or last_seen is None:
        return True
    if absolute_days and now - created_at >= absolute_days * DAY_SECONDS:
        return True
    if idle_days and now - last_seen >= idle_days * DAY_SECONDS:
        return True
    return False


def _as_timestamp(value):
    """把时间戳列转成 float；不可用时返回 None（调用方按"已过期"处理）。

    接受 int/float 与"看起来像数字的字符串"（SQLite 动态类型可能存成 TEXT），
    拒绝 NaN / inf —— 它们参与比较的结果不可预测。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, (str, bytes)):
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if candidate != candidate or candidate in (float("inf"), float("-inf")):
        return None                                   # NaN / ±inf
    return candidate


def create_session(user_id: int) -> str:
    """为用户颁发新会话 token 并入库，返回 token（由调用方写入 Cookie）。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with write_tx() as conn:
        # 顺带清理已过期会话（低成本，不需要额外定时任务）
        _delete_expired(conn, now)
        conn.execute(
            "INSERT OR REPLACE INTO session "
            "(token, user_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now)
        )
    return token


def get_session_user(token: str):
    """按 token 查会话所属用户 id；过期会话顺手删除并视为未登录。

    有效访问会刷新 `last_seen`（滑动窗口）——这正是"常用设备不被踢"的来源。

    为什么**整段**放在 `write_tx()` 里（而不是"先 connect 读、再 write_tx 写"）：
    这是"读取 + 业务判断 + 写入"的复合操作，判定（是否过期、要不要续期）与
    相应的写入必须处于同一临界区，否则判断依据可能在两者之间被别的进程改掉。
    代价是带会话 Cookie 的请求会取一次写锁 —— 这是**既有设计**（滑动过期要写
    `last_seen`）决定的，不是这次改动引入的；没有会话 Cookie 的匿名请求
    在第一行就返回，完全不碰数据库。
    """
    if not token:
        return None
    absolute_days, idle_days = _session_limits()
    now = time.time()
    with write_tx() as conn:
        row = conn.execute(
            "SELECT user_id, created_at, last_seen FROM session WHERE token = ?",
            (token,)
        ).fetchone()
        if not row:
            return None
        if _is_expired(row, absolute_days, idle_days, now):
            conn.execute("DELETE FROM session WHERE token = ?", (token,))
            return None
        conn.execute("UPDATE session SET last_seen = ? WHERE token = ?", (now, token))
        return row["user_id"]


def delete_session(token: str):
    """注销单个会话（退出登录时使用）。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM session WHERE token = ?", (token,))


def delete_user_sessions(user_id: int):
    """删除某用户全部会话：登录防会话固定、改密后强制重新登录、删号时使用。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))


def _delete_expired(conn, now: float) -> int:
    """在当前连接上删除已过期会话，返回删除行数。

    判定条件与 `_is_expired()` 一一对应，避免"清理用一套、校验用另一套"
    导致某些行永远清不掉。
    """
    absolute_days, idle_days = _session_limits()
    clauses = []
    params = []
    if absolute_days:
        clauses.append("created_at IS NULL OR ? - created_at >= ?")
        params.extend([now, absolute_days * DAY_SECONDS])
    if idle_days:
        clauses.append("last_seen IS NULL OR ? - last_seen >= ?")
        params.extend([now, idle_days * DAY_SECONDS])
    if not clauses:
        return 0                      # 两个维度都关了：没有"过期"可言
    cursor = conn.execute(f"DELETE FROM session WHERE {' OR '.join(clauses)}", params)
    return cursor.rowcount or 0


def cleanup_expired_sessions():
    """启动时清理全部过期会话（与 comment_rate 清理任务同一风格）。"""
    with write_tx() as conn:
        removed = _delete_expired(conn, time.time())
    return removed
