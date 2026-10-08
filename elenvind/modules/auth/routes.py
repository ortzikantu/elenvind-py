"""Auth 模块：登录、注册、登出。

密码 / 会话 / CSRF / Cookie 全部由 Core 负责：
- 校验密码 -> `core.auth.verify_credentials`（含透明 rehash）
- 颁发会话 -> `core.session.login_user`（含防会话固定轮换）
- CSRF      -> 调度器默认施加（本模块**不写**任何 CSRF 代码）
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from ...core.auth import verify_credentials
from ...core.config import config
from ...core.context import current_lang
# 捕获"邮箱已存在"要用的异常类型：由 Core 重新导出，模块不需要（也不该）
# import sqlite3 —— 见 Core Contract 守卫。
from ...db import IntegrityError
from ... import db
from ...db.auth import complete_login_success, reserve_login_attempt, try_register_attempt
from ...db.user import create_user, get_user_by_email, update_user_password
from ...core.http import html, redirect, safe_next_path
from ...core.i18n import t
from ...core.security import (
    EMAIL_MAX,
    NICKNAME_MAX,
    PASSWORD_MAX,
    PASSWORD_MIN,
    dummy_verify,
    hash_password,
)
from ...core.session import login_user, logout_user
from ...core.templating import render_template
from ...db.user import normalize_email

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


def register(router):
    """把认证路由装到 router 上。

    曾经这里收 `render_forbidden=` 参数，但它**从未被使用**：
    认证闸门的 403 是 Core 的 `router.dispatch(forbidden=...)` 处理的，
    模块只需要声明 `auth=` / `permission=`。一个收下却永远不用的参数
    会让读者以为"权限被拒时会走这里"，从而在错误的地方排查问题。
    """
    @router.route("/login", methods=["GET", "POST"])
    def login(request):
        lang = current_lang()
        message = ""
        # 回跳地址：来自认证闸门的 `?next=`，只接受站内路径（防开放重定向）
        next_path = safe_next(request.form.get("next") or request.arg("next"))
        if request.method == "POST":
            email = normalize_email(request.form.get("email", ""))
            password = request.form.get("password", "")
            message, retry_after = _try_login(request, email, password, lang)
            if message is None:
                # 会话 Cookie 与 CSRF Cookie 由 Core 的 send_response 统一下发
                # （模块不拼 Cookie 名，因此 __Host- 前缀之类的策略自动生效）
                return redirect(next_path or "/")
        else:
            retry_after = 0
        response = html(render_template("auth/login.html", {
            "message": message, "message_kind": "error",
            "limits": input_limits(),
            "next_path": next_path,
        }))
        if retry_after:
            # 被限流时明确告诉客户端还要等多久（不再是一句模糊的"稍后再试"）
            response.headers.append((b"Retry-After", str(int(retry_after)).encode("ascii")))
        return response

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
        # 只调 Core：它负责删服务端会话**并**让浏览器 Cookie 立即过期。
        # 模块不 delete_cookie、不 import SESSION_COOKIE（否则既知道 Cookie
        # 名字、又得自己处理 __Host- 前缀，两套策略迟早漂移）。
        logger.info("Logout: user_id=%s ip=%s", request.user["id"], request.client_ip)
        logout_user(request, store=db)
        return redirect("/")

    return router


def safe_next(raw) -> str:
    """把回跳地址规范化成**站内路径**；不合法一律返回空串。

    实现收敛到 Core 的 `core.http.safe_next_path`（全项目唯一一处）。
    **不要**在模块里再写一份：历史上存在四份严格程度不同的实现，
    其中 `/theme` 那份不拒绝反斜杠，而四份都只拒绝 CR/LF、**都不拒绝 TAB**，
    于是 `/\t/evil.com` 经浏览器解析（URL 标准会先剥离 TAB）变成
    `//evil.com` —— 登录后跨站跳转。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    value = safe_next_path(raw, default="")
    # Core 用 "/" 表示"回退到首页"；本函数的契约是用空串表示"没有合法目标"
    return "" if (value == "/" and raw.strip() != "/") else value


def _lock_message(lang, reason, retry_after: int):
    """限流提示：基础文案 + 由渐进 backoff 算出的等待时间。

    历史上提示里硬编码了"24 小时"，而窗口是可配置的 —— 改配置后文案就在撒谎。
    现在等待时间来自真实判定（`db.reserve_login_attempt` 的返回值）。
    """
    keys = {"global": "auth_err_global", "email": "auth_err_email_lock",
            "ip": "auth_err_ip_lock"}
    base = _message(lang, keys.get(reason, "auth_err_global"))
    minutes = max(1, (int(retry_after) + 59) // 60)
    return f"{base} {t(lang, 'auth_err_retry_after', minutes=minutes)}"


def _try_login(request, email, password, lang):
    """返回 `(message | None, retry_after)`；`message is None` 表示登录成功。

    流程（顺序是安全语义的一部分）：

        1. 一次**短写事务**：三闸门判定 + 占位记账（并发不可能超发）
        2. 释放 SQLite 写锁，再做昂贵的 scrypt 校验（不占锁）
        3. 成功 → 一次短写事务收尾（清失败流水 + 审计 + 可选 rehash）
    """
    ip = request.client_ip or "unknown"
    limits = login_limits()
    if len(email) > EMAIL_MAX or not email or len(password) > PASSWORD_MAX:
        return _message(lang, "auth_err_creds"), 0

    # 1) 判定 + 占位：旧实现在这里读三次计数（各自独立连接），
    #    中间还夹着 scrypt，并发请求会同时读到低计数而全部放行。
    allowed, reason, retry_after = reserve_login_attempt(
        email, ip,
        max_email_failures=limits["max_email_failures"],
        email_window_seconds=limits["email_window_seconds"],
        max_ip_failures=limits["max_ip_failures"],
        ip_window_seconds=limits["ip_window_seconds"],
        max_global_failures=limits["max_global_failures"],
        global_window_seconds=limits["global_window_seconds"],
    )
    if not allowed:
        # 三个闸门都记 WARNING：它们是"有人在爆破"的唯一信号来源
        logger.warning("Login blocked (%s failure limit): ip=%s retry_after=%ss",
                       reason, ip, retry_after)
        return _lock_message(lang, reason, retry_after), retry_after

    # 2) 写锁已释放，才做密码校验（scrypt 约 240ms，不能让写锁陪着等）
    user = get_user_by_email(email)
    ok, rehashed_hash = verify_credentials(user, password)
    if ok:
        # 3) 收尾：清该邮箱失败流水 + 记成功 +（必要时）落新哈希，同一事务
        complete_login_success(email, ip, user_id=user["id"],
                               new_password_hash=rehashed_hash)
        login_user(request, user["id"], store=db)   # 轮换会话 + 写 Cookie（store 注入）
        logger.info("Login succeeded: user_id=%s ip=%s", user["id"], ip)
        return None, 0

    # 账号不存在时也做一次等量哈希校验，弱化计时侧信道（Core 之外只此一处）
    if user is None:
        dummy_verify(password)
    # 失败无需再记账：第 1 步的占位就是这次失败
    logger.info("Login failed: ip=%s", ip)
    return _message(lang, "auth_err_creds"), 0


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
        logger.info("Registration rejected (email already registered): ip=%s",
                    request.client_ip)
        return _message(lang, "auth_err_register_failed")
    try:
        new_user_id = create_user(nickname, email, hash_password(password))
    except IntegrityError:
        # 并发注册撞上同一邮箱：UNIQUE 约束给出最终判定，返回友好提示而不是 500
        logger.info("Registration rejected (email already registered): ip=%s",
                    request.client_ip)
        return _message(lang, "auth_err_register_failed")
    logger.info("Registration succeeded: user_id=%s ip=%s",
                new_user_id, request.client_ip)
    return None
