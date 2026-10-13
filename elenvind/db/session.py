"""session 表：服务端会话的存取（登录发证 / 鉴权查证 / 注销删除）。

安全说明：
- token 是 32 字节 cryptographically-secure 随机串，仅存在于服务端表与用户 Cookie 中，
  攻击者无法预测，也不像 JWT 那样携带可伪造的载荷。
- 登录成功由 `rotate_session()` 在**一个写事务**里作废该账号全部旧会话并颁发新证
  （防会话固定；两段式实现会让并发登录各自留下一个有效会话）。
- 发证与校验都会顺带清理过期会话，避免长运行后表无限增长。
- **读写分路径**：`get_session_user()` 走只读连接（`connect()`，不参与 flock），
  只有"刷新 `last_seen`"与"删除过期行"才升级到 `write_tx()`
  （flock 保证跨 worker 互斥，BEGIN IMMEDIATE 保证事务原子性）。

过期策略（两个维度，都需要满足才有效）：
- **绝对过期** `created_at + absolute_days`：会话无论多活跃，活够这么久就必须重新登录。
  这是"token 泄露后风险窗口"的硬上限。
- **滑动过期** `last_seen + idle_days`：闲置超过这么久即失效。
  每次有效访问会刷新 `last_seen`，所以常用设备不会被踢。
两者任一超时都视为无效，并**当场删除**该行（不给重复利用留机会）。
阈值来自 config.toml（`session_absolute_days` / `session_idle_days`），
设为 0 表示该维度不过期——那会显著放大 token 泄露的风险，站长需自行权衡。
"""
import math
import secrets
import time

from .connection import connect
from .transaction import write_tx

#: 兜底默认值（配置缺失时使用）。config.toml 里的同名键是实际生效来源。
DEFAULT_ABSOLUTE_DAYS = 30
DEFAULT_IDLE_DAYS = 15

DAY_SECONDS = 86400

#: 滑动过期刷新的最小间隔（秒）。
#: 窗口以"天"计，秒级精度毫无意义；而不节流的话，**任何**带会话 Cookie 的请求
#: （包括每一个静态资源）都会取一次跨进程写锁并提交一次 WAL —— 那既是吞吐上限，
#: 也是一条"单个已登录用户即可独占全局写锁"的放大面。
LAST_SEEN_REFRESH_SECONDS = 300

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
    expired_absolute = bool(absolute_days) and now - created_at >= absolute_days * DAY_SECONDS
    expired_idle = bool(idle_days) and now - last_seen >= idle_days * DAY_SECONDS
    return expired_absolute or expired_idle

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
    if math.isnan(candidate) or candidate in (float("inf"), float("-inf")):
        return None                                   # NaN / ±inf
    return candidate


def _insert_session(conn, user_id: int, now: float) -> str:
    """在给定连接上插入一条新会话，返回 token（调用方负责事务边界）。"""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT OR REPLACE INTO session "
        "(token, user_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
        (token, user_id, now, now),
    )
    return token


def create_session(user_id: int) -> str:
    """为用户颁发新会话 token 并入库，返回 token（不清除该用户已有会话）。"""
    now = time.time()
    with write_tx() as conn:
        # 顺带清理已过期会话（低成本，不需要额外定时任务）
        _delete_expired(conn, now)
        token = _insert_session(conn, user_id, now)
    return token


def rotate_session(user_id: int) -> str:
    """作废该账号**全部**旧会话并颁发新 token —— **同一个写事务**。

    登录/提权专用（防会话固定）。为什么必须是一个事务：
    旧实现是"删除全部旧会话"与"插入新会话"两次独立写事务，
    两个并发登录会交错成"各自留下一行会话"（实测 session 行数 = 2）。
    合并后，同一账号在任何并发下都只剩最后颁发的那一个会话。
    """
    now = time.time()
    with write_tx() as conn:
        _delete_expired(conn, now)
        conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))
        token = _insert_session(conn, user_id, now)
    return token


def touch_session(token: str, now: float | None = None) -> None:
    """刷新 `last_seen`（滑动过期）。由 `get_session_user()` 按节流调用。"""
    with write_tx() as conn:
        conn.execute("UPDATE session SET last_seen = ? WHERE token = ?",
                     (time.time() if now is None else now, token))


def get_session_user(token: str):
    """按 token 查会话所属用户 id；过期会话顺手删除并视为未登录。

    **读取走只读路径**（`connect()`，不参与 flock）：绝大多数带会话的请求
    只是问"这条会话还有效吗"，不该为此 acquire 全局写锁 —— 实测那会让静态
    资源吞吐掉到一半（1961 rps → 1015 rps），并让所有写操作排在同一个瓶颈后。
    只有真的需要写入时才升级到 `write_tx()`：

    - 会话已过期 → 删除该行（`delete_session`）；
    - `last_seen` 超过 `LAST_SEEN_REFRESH_SECONDS` → 刷新它（`touch_session`）。

    并发下最坏情形是多个线程同时刷新到相近的 `now`（幂等，无副作用），
    或者对一个刚被别的进程删掉的会话多做一次 0 行的 DELETE —— 都不影响语义。
    匿名请求（没有会话 Cookie）在第一行就返回，完全不碰数据库。
    """
    if not token:
        return None
    absolute_days, idle_days = _session_limits()
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, created_at, last_seen FROM session WHERE token = ?",
            (token,)
        ).fetchone()
    if not row:
        return None
    if _is_expired(row, absolute_days, idle_days, now):
        delete_session(token)
        return None
    last_seen = _as_timestamp(row["last_seen"])
    if last_seen is None or now - last_seen >= LAST_SEEN_REFRESH_SECONDS:
        touch_session(token, now)
    return row["user_id"]


def delete_session(token: str):
    """注销单个会话（退出登录时使用）。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM session WHERE token = ?", (token,))


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
