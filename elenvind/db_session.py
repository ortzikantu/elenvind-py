"""session 表：服务端会话的存取（登录发证 / 鉴权查证 / 注销删除）。

安全说明：
- token 是 32 字节 cryptographically-secure 随机串，仅存在于服务端表与用户 Cookie 中，
  攻击者无法预测，也不像 JWT 那样携带可伪造的载荷。
- 登录成功会先 delete_user_sessions(user_id) 再发新证，防止会话固定攻击。
- 每次发证顺带清理过期会话，避免长运行后表无限增长。
"""
import secrets
import time

from .db_base import get_connection

# 会话有效期：与 http_base 中 Cookie 的 max-age 保持同一数值（7 天）
SESSION_DAYS = 7


def create_session(user_id: int, days: int = SESSION_DAYS) -> str:
    """为用户颁发新会话 token 并入库，返回 token（由调用方写入 Cookie）。"""
    token = secrets.token_urlsafe(32)
    expires = time.time() + days * 86400
    conn = get_connection()
    # 顺带清理已过期会话（低成本，不需要额外定时任务）
    conn.execute("DELETE FROM session WHERE expires < ?", (time.time(),))
    conn.execute(
        "INSERT OR REPLACE INTO session (token, user_id, expires) VALUES (?, ?, ?)",
        (token, user_id, expires)
    )
    conn.commit()
    conn.close()
    return token


def get_session_user(token: str):
    """按 token 查会话所属用户 id；过期会话顺手删除并视为未登录。"""
    conn = get_connection()
    row = conn.execute("SELECT user_id, expires FROM session WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not row:
        return None
    if row["expires"] < time.time():
        delete_session(token)
        return None
    return row["user_id"]


def delete_session(token: str):
    """注销单个会话（退出登录时使用）。"""
    conn = get_connection()
    conn.execute("DELETE FROM session WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def delete_user_sessions(user_id: int):
    """删除某用户全部会话：登录防会话固定、改密后强制重新登录、删号时使用。"""
    conn = get_connection()
    conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def cleanup_expired_sessions():
    """启动时清理全部过期会话。"""
    conn = get_connection()
    conn.execute("DELETE FROM session WHERE expires < ?", (time.time(),))
    conn.commit()
    conn.close()
