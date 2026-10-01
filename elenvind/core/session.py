"""Core Session：服务端会话的唯一 API。

    Feature ──► current_user(request) / login_user() / logout_user()
    Core    ──► Core 管理 Cookie、轮换、失效；Feature 不碰 session cookie。

安全语义（集中且唯一）：
- 会话是 32 字节随机 token，只存服务端表里，客户端只拿 Cookie；
- **登录必须轮换**：先删除该账号全部旧会话再发新证（防会话固定）；
- 改密/删号删除该账号全部会话，并清除浏览器 Cookie；
- 过期会话读取时即删除；
- Cookie 属性（HttpOnly/SameSite=Lax/Secure/Path=）由 Core 统一设置。
"""
from __future__ import annotations

from .db_session import (
    SESSION_DAYS,
    cleanup_expired_sessions,
    create_session,
    delete_session,
    delete_user_sessions,
    get_session_user,
)
from .db_user import get_user_by_id
from .security import SESSION_MAX_AGE

__all__ = [
    "SESSION_DAYS", "SESSION_MAX_AGE",
    "load_user", "current_user", "login_user", "logout_user",
    "rotate_session", "invalidate_user_sessions", "cleanup_expired_sessions",
    "get_session_user", "delete_session",
]


def load_user(request):
    """把会话里的用户挂到 request 上（每次请求调用一次）。

    返回用户行对象或 None。Feature 用 `current_user(request)` 取。
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

    这是 Feature 获取用户的**唯一**方式：不允许自己读会话 Cookie。

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
    """
    delete_user_sessions(user_id)
    token = create_session(user_id)
    request.session_token = token
    return token


def login_user(request, user_id: int):
    """完成登录：轮换会话并把用户挂到请求上。返回新 token。"""
    token = rotate_session(request, user_id)
    request.user = get_user_by_id(user_id)
    return token


def logout_user(request):
    """登出：删除服务端会话并让 Cookie 立即过期。"""
    if request is not None and request.session_token:
        delete_session(request.session_token)
        request.session_token = None
    if request is not None:
        request.user = None


def invalidate_user_sessions(user_id: int):
    """改密/删号：删除该账号全部会话（调用方随后清浏览器 Cookie）。"""
    delete_user_sessions(user_id)
