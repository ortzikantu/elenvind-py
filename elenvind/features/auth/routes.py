"""Auth Feature：登录、注册、登出。

密码 / 会话 / CSRF / Cookie 全部由 Core 负责：
- 校验密码 -> `core.auth.verify_credentials`（含透明 rehash）
- 颁发会话 -> `core.session.login_user`（含防会话固定轮换）
- CSRF      -> 调度器默认施加（本模块**不写**任何 CSRF 代码）
"""
from __future__ import annotations

import logging
import sqlite3
from urllib.parse import quote

from ...core.auth import verify_credentials
from ...core.config import config
from ...core.context import current_lang
from ...core.db_register import try_register_attempt
from ...core.db_user import create_user, get_user_by_email
from ...core.http import html, redirect
from ...core.i18n import t
from ...core.security import (
    EMAIL_MAX,
    NICKNAME_MAX,
    PASSWORD_MAX,
    PASSWORD_MIN,
    SESSION_COOKIE,
    dummy_verify,
    hash_password,
)
from ...core.session import login_user, logout_user
from ...core.templating import render_template
from ...core.utils import normalize_email

logger = logging.getLogger(__name__)

#: 登录限流默认阈值（可被 [login_limits] 覆盖）
DEFAULT_LOGIN_LIMITS = {
    "max_email_failures": 5,
    "email_window_seconds": 24 * 3600,
    "max_ip_failures": 20,
    "ip_window_seconds": 15 * 60,
    "max_global_failures": 200,
    "global_window_seconds": 15 * 60,
}
#: 注册限流默认阈值
DEFAULT_REGISTER_LIMITS = {"max_per_ip": 5, "window_seconds": 3600}


def login_limits():
    return _limits("login_limits", DEFAULT_LOGIN_LIMITS)


def register_limits():
    return _limits("register_limits", DEFAULT_REGISTER_LIMITS)


def _limits(section, defaults):
    raw = config.get(section) or {}
    limits = dict(defaults)
    if isinstance(raw, dict):
        for key in limits:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


def registration_enabled() -> bool:
    return bool(config.get("registration_enabled", True))


def input_limits():
    """表单字段长度限制（模板与后端共用同一组值）。"""
    return {
        "email": EMAIL_MAX,
        "nickname": NICKNAME_MAX,
        "password": PASSWORD_MAX,
        "password_min": PASSWORD_MIN,
    }


def _message(lang, key):
    return t(lang, key)


def register(router, *, render_forbidden=None):
    """把认证路由装到 router 上。"""
    @router.route("/login", methods=["GET", "POST"])
    def login(request):
        lang = current_lang()
        message = ""
        # 回跳地址：来自认证闸门的 `?next=`，只接受站内路径（防开放重定向）
        next_path = safe_next(request.form.get("next") or request.arg("next"))
        if request.method == "POST":
            email = normalize_email(request.form.get("email", ""))
            password = request.form.get("password", "")
            message = _try_login(request, email, password, lang)
            if message is None:
                # 会话 Cookie 与 CSRF Cookie 由 Core 的 send_response 统一下发
                # （Feature 不拼 Cookie 名，因此 __Host- 前缀之类的策略自动生效）
                return redirect(next_path or "/")
        return html(render_template("auth/login.html", {
            "message": message, "message_kind": "error",
            "limits": input_limits(),
            "next_path": next_path,
        }))

    @router.route("/register", methods=["GET", "POST"])
    def register(request):
        lang = current_lang()
        message = ""
        next_path = safe_next(request.form.get("next") or request.arg("next"))
        if request.method == "POST":
            result = _try_register(request, lang)
            if result is None:
                # 注册成功 -> 去登录，并把回跳地址继续带着
                return redirect(f"/login?next={quote(next_path, safe='')}"
                                if next_path else "/login")
            message = result
        return html(render_template("auth/register.html", {
            "message": message, "message_kind": "error",
            "limits": input_limits(),
            "next_path": next_path,
        }))

    @router.route("/logout", methods=["GET"])
    def logout_confirm(request):
        """友好的退出确认页。

        **只读**：不在 GET 里销毁会话（那会让退出变成可被 `<img src>` 触发的
        状态变更）。真正的退出由下面的 POST + CSRF 完成。
        未登录时（或会话已失效）也走到这里，页面渲染"你已退出登录"。
        """
        return html(render_template("auth/logout.html", {}))

    @router.route("/logout", methods=["POST"], auth="required")
    def logout(request):
        logout_user(request)
        response = redirect("/")
        response.delete_cookie(SESSION_COOKIE)
        return response

    return router


def safe_next(raw) -> str:
    """把回跳地址规范化成**站内路径**；不合法一律返回空串。

    拒绝：非字符串、不以内 `/` 开头、协议相对（`//host`）、含 CR/LF、
    反斜杠（浏览器会把它当斜杠，可能变成协议相对 URL）。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    value = raw.strip()
    if not value.startswith("/") or value.startswith("//"):
        return ""
    if "\\" in value or "\r" in value or "\n" in value:
        return ""
    return value


def _try_login(request, email, password, lang):
    """返回 None 表示成功；否则返回错误文案。"""
    from ...core.db_login import (
        clear_login_attempts,
        count_email_failures,
        count_global_recent_failures,
        count_ip_failures,
        record_login_attempt,
    )

    ip = request.client_ip or "unknown"
    limits = login_limits()
    if len(email) > EMAIL_MAX or not email or len(password) > PASSWORD_MAX:
        return _message(lang, "auth_err_creds")
    if count_global_recent_failures(limits["global_window_seconds"]) >= limits["max_global_failures"]:
        return _message(lang, "auth_err_global")
    if count_email_failures(email, limits["email_window_seconds"]) >= limits["max_email_failures"]:
        return _message(lang, "auth_err_email_lock")
    if count_ip_failures(ip, limits["ip_window_seconds"]) >= limits["max_ip_failures"]:
        return _message(lang, "auth_err_ip_lock")

    user, ok, _rehashed = verify_credentials(email, password)
    if ok:
        clear_login_attempts(email)
        record_login_attempt(email, ip, success=True)
        login_user(request, user["id"])       # Core：轮换会话 + 写 Cookie
        return None

    # 账号不存在时也做一次等量哈希校验，弱化计时侧信道（Core 之外只此一处）
    if get_user_by_email(email) is None:
        dummy_verify(password)
    record_login_attempt(email, ip, success=False)
    return _message(lang, "auth_err_creds")


def _try_register(request, lang):
    """返回 None 表示注册成功；否则返回错误文案。"""
    if not registration_enabled():
        return _message(lang, "auth_err_register_disabled")

    nickname = request.form.get("nickname", "").strip()
    email = normalize_email(request.form.get("email", ""))
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not nickname or not email or not password:
        return _message(lang, "auth_err_required")
    if len(nickname) > NICKNAME_MAX:
        return _message(lang, "auth_err_register_failed")
    if len(email) > EMAIL_MAX or "@" not in email:
        return _message(lang, "auth_err_register_failed")
    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        return _message(lang, "auth_err_password_range")
    if password != confirm:
        return _message(lang, "auth_err_password_mismatch")

    limits = register_limits()
    if not try_register_attempt(request.client_ip or "unknown",
                                max_per_ip=limits["max_per_ip"],
                                window_seconds=limits["window_seconds"]):
        logger.warning("Registration rate limit hit: ip=%s", request.client_ip)
        return _message(lang, "auth_err_register_rate")

    if get_user_by_email(email):
        return _message(lang, "auth_err_register_failed")
    try:
        create_user(nickname, email, hash_password(password))
    except sqlite3.IntegrityError:
        # 并发注册撞上同一邮箱：UNIQUE 约束给出最终判定，返回友好提示而不是 500
        return _message(lang, "auth_err_register_failed")
    return None
