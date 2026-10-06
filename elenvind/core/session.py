"""Core Session：服务端会话的唯一 API。

    模块 ──► current_user(request) / login_user() / logout_user()
    Core    ──► Core 管理 Cookie、轮换、失效；模块不碰 session cookie。

安全语义（集中且唯一）：
- 会话是 32 字节随机 token，只存服务端表里，客户端只拿 Cookie；
- **登录必须轮换**：先删除该账号全部旧会话再发新证（防会话固定）；
- 改密/删号删除该账号全部会话，并清除浏览器 Cookie；
- **两级过期**（阈值见 config.toml，规则与实现都在 db_session）：
  绝对过期 `session_absolute_days`（默认 30 天）——
    token 泄露后的风险窗口硬上限，与活跃程度无关；
  滑动过期 `session_idle_days`（默认 15 天）——
    闲置超时即失效，有效访问会刷新 last_seen；
  过期会话在读取时即删除，启动时也会统一清理一遍；
- Cookie 属性（HttpOnly/SameSite=Lax/Secure/Path=）由 Core 统一设置。
"""
from __future__ import annotations

from .db_session import (
    DEFAULT_ABSOLUTE_DAYS,
    DEFAULT_IDLE_DAYS,
    cleanup_expired_sessions,
    create_session,
    delete_session,
    delete_user_sessions,
    get_session_user,
)
from .db_user import get_user_by_id
from .security import SESSION_MAX_AGE

__all__ = [
    "DEFAULT_ABSOLUTE_DAYS", "DEFAULT_IDLE_DAYS", "SESSION_MAX_AGE",
    "load_user", "current_user", "login_user", "logout_user",
    "rotate_session", "invalidate_user_sessions", "cleanup_expired_sessions",
    "get_session_user", "delete_session", "session_cookie_max_age",
]


def session_cookie_max_age() -> int:
    """会话 Cookie 的 max-age（秒）。

    取**绝对过期**窗口：它决定"这个 Cookie 在浏览器侧最多能留多久"。
    滑动过期比它短，没必要把 Cookie 留得更久。

    注意这只是浏览器侧的清理提示，真正的判定在服务端；
    配置成 0（绝对不过期）时回落到 SESSION_MAX_AGE，避免下发一个
    "立即过期"的 Cookie。
    """
    from .config import config

    try:
        days = max(int(config.get("session_absolute_days", DEFAULT_ABSOLUTE_DAYS)), 0)
    except (TypeError, ValueError):
        days = DEFAULT_ABSOLUTE_DAYS
    return days * 86400 if days else SESSION_MAX_AGE


def load_user(request):
    """把会话里的用户挂到 request 上（每次请求调用一次）。

    返回用户行对象或 None。模块用 `current_user(request)` 取。
    """
    token = request.session_token
    if not token:
        request.user = None
        return None
    user_id = get_session_user(token)
    if not user_id:
        request.user = None
        return None
    user = get_user_by_id(user_id)
    request.user = user
    return user


def current_user(request=None):
    """当前登录用户；无请求上下文或未登录返回 None。

    这是模块获取用户的**唯一**方式：不允许自己读会话 Cookie。

    注意：用户对象是 `sqlite3.Row`，**不支持 getattr**（`getattr(row, "x")` 恒为
    None），必须用下标访问。
    """
    if request is None:
        from .context import current_request
        request = current_request()
    if request is None:
        return None
    try:
        return request.user
    except AttributeError:
        return None


def rotate_session(request, user_id: int):
    """登录/提权专用：作废该账号全部旧会话，颁发全新会话。

    防会话固定：绝不把"匿名会话"原地升级成登录会话。
    返回新 token（由 Core 写进 Cookie）。

    必须走 `request.set_session_token()`（而不是直接赋值）：它同时把
    "Cookie 需要下发"标脏，`pending_cookies()` 才会真正发出 Set-Cookie。
    """
    delete_user_sessions(user_id)
    token = create_session(user_id)
    request.set_session_token(token)
    return token


def login_user(request, user_id: int):
    """完成登录：轮换会话并把用户挂到请求上。返回新 token。"""
    token = rotate_session(request, user_id)
    request.user = get_user_by_id(user_id)
    return token


def logout_user(request):
    """登出：删除服务端会话，并让浏览器 Cookie 立即过期。

    Cookie 的清除由 Core 统一完成（`request.invalidate_session_cookie()`
    会把 token 置空并标记"需要清除 Cookie"，`Request.pending_cookies()` 负责
    下发清除指令）。**模块不需要、也不应该自己去 delete_cookie()** ——
    它既不该知道 Cookie 名字，也不该知道 `__Host-` 前缀这类策略。
    """
    if request is not None:
        request.invalidate_session_cookie()


def invalidate_user_sessions(user_id: int, request=None):
    """改密 / 删号：删除该账号**全部**会话。

    传入 `request` 时同时清掉当前浏览器会话（否则当前请求结束时
    `pending_cookies()` 会把已作废的 token 又写回 Cookie，把显式删除抵消掉）。
    """
    delete_user_sessions(user_id)
    if request is not None:
        request.invalidate_session_cookie()
