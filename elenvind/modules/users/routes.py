"""Users 模块：个人中心（资料查看/修改、改密、删号）。

认证由 Core 完成（`auth="required"`）；密码与会话操作由 Core 提供：
- 改密 -> `core.db_user.update_user_password` + `core.security.hash_password`，
  随后 `core.session.invalidate_user_sessions()` 强制全端重登（Core 语义）；
- 删号 -> 逻辑删除 + 清理全部会话。
本模块不写任何 session cookie / CSRF / 密码逻辑。
"""
from __future__ import annotations

import logging
from datetime import datetime

from ...core.context import current_lang
# 捕获"邮箱被他人抢先占用"要用的异常类型：由 Core 重新导出，
# 模块不需要（也不该）import sqlite3 —— 见 Core Contract 守卫。
from ...core.db_base import IntegrityError
from ...core.db_user import (
    delete_user,
    get_user_by_email,
    update_user_password,
    update_user_profile,
)
from ...core.http import html, redirect
from ...core.i18n import t
from ...core.security import (
    EMAIL_MAX,
    NICKNAME_MAX,
    PASSWORD_MAX,
    PASSWORD_MIN,
    hash_password,
    verify_password,
)
from ...core.session import invalidate_user_sessions
from ...core.templating import render_template
from ...core.utils import format_datetime, normalize_email

logger = logging.getLogger(__name__)

NICKNAME_CHANGE_INTERVAL_DAYS = 365


def register(router):
    """把个人中心路由装到 router 上。

    不收 `render_forbidden=`：权限被拒的 403 由 Core 的
    `router.dispatch(forbidden=...)` 统一处理，这里的路由只声明 `auth=`。
    收下一个永不使用的参数只会误导排查方向。
    """
    @router.route("/user", methods=["GET"])
    def profile_page(request):
        """个人中心页面。

        GET 是**公开**的：未登录时渲染友好的"请先登录"页（而不是 403）。
        真正的写操作在下面的 POST 路由上，由 Core 的 `auth="required"` 保护。
        """
        user = request.user
        if user is None:
            from ...core.config import config
            return html(render_template("users/profile.html", {
                "message": "", "message_kind": "error",
                "limits": _input_limits(),
                "profile": None,
                # 注册关闭时不显示注册入口（避免点了才知道关着）
                "registration_enabled": bool(config.get("registration_enabled", True)),
            }))
        return html(_render_profile(request, user, "", "error"))

    @router.route("/user", methods=["POST"], auth="required")
    def profile_update(request):
        user = request.user
        lang = current_lang()
        message = ""
        kind = "error"
        action = request.form.get("action", "")

        if action == "update_profile":
            message, user = _update_profile(request, user, lang)
            if not message:
                # 成功时给出明确反馈，而不是渲染一个"看起来没反应"的表单
                message = t(lang, "user_ok_profile")
                kind = "success"
                logger.info("Profile updated: user_id=%s", user["id"])
        elif action == "change_password":
            result = _change_password(request, user, lang)
            if result is None:
                # Core 语义：改密后该账号全部会话失效，强制重新登录。
                # 传入 request 让 Core 同时清除浏览器 Cookie ——
                # 模块不 delete_cookie、不 import SESSION_COOKIE。
                invalidate_user_sessions(user["id"], request=request)
                logger.info("Password changed: user_id=%s ip=%s",
                            user["id"], request.client_ip)
                return redirect("/login")
            message = result
        elif action == "delete_account":
            password_confirm = request.form.get("password_confirm", "")
            if not verify_password(password_confirm, user["password"]):
                message = t(lang, "user_err_password_incorrect")
                logger.warning("Account deletion rejected (wrong password): "
                               "user_id=%s ip=%s", user["id"], request.client_ip)
            else:
                delete_user(user["id"])
                invalidate_user_sessions(user["id"], request=request)
                logger.info("Account deleted: user_id=%s ip=%s",
                            user["id"], request.client_ip)
                return redirect("/")
        else:
            message = t(lang, "user_err_unknown_action")
            logger.warning("Unknown profile action: user_id=%s action=%s",
                           user["id"], action)

        return html(_render_profile(request, user, message, kind))

    return router


def _input_limits():
    return {
        "email": EMAIL_MAX,
        "nickname": NICKNAME_MAX,
        "password": PASSWORD_MAX,
        "password_min": PASSWORD_MIN,
    }


def _render_profile(request, user, message, kind):
    return render_template("users/profile.html", {
        "message": message,
        "message_kind": kind,
        "limits": _input_limits(),
        "profile": {"registered_at": format_datetime(user["created_at"])},
    })


def _update_profile(request, user, lang):
    """返回 (message, user)；message 为空表示成功。"""
    new_nickname = request.form.get("nickname", "").strip()
    new_email = normalize_email(request.form.get("email", ""))
    current_password = request.form.get("current_password", "")

    if not new_nickname or not new_email:
        return t(lang, "user_err_empty"), user
    if len(new_nickname) > NICKNAME_MAX:
        return t(lang, "user_err_nickname_long"), user
    if len(new_email) > EMAIL_MAX or "@" not in new_email:
        return t(lang, "user_err_email_invalid"), user

    email_changed = new_email != normalize_email(user["email"])
    if email_changed and not verify_password(current_password, user["password"]):
        return t(lang, "user_err_password_for_email"), user

    existing = get_user_by_email(new_email)
    if existing and existing["id"] != user["id"]:
        return t(lang, "user_err_email_used"), user

    nickname_changed = new_nickname != user["nickname"]
    if nickname_changed:
        error = _nickname_change_error(user, lang)
        if error:
            return error, user

    try:
        # 一个事务里同时改昵称与邮箱：否则并发抢邮箱时会出现
        # "邮箱没改成功、昵称却已经改了"的半写状态（见 update_user_profile）。
        update_user_profile(
            user["id"],
            nickname=new_nickname if nickname_changed else None,
            email=new_email if email_changed else None,
        )
    except IntegrityError:
        # 并发下邮箱被他人抢先占用：UNIQUE 约束是最终权威
        return t(lang, "user_err_email_used"), user

    updated = dict(user)
    updated["nickname"] = new_nickname
    updated["email"] = new_email
    if nickname_changed:
        updated["nickname_changed_at"] = datetime.now().isoformat()
    return "", updated


def _nickname_change_error(user, lang):
    """昵称一年只能改一次；时间字段损坏时视为无限制（只捕预期异常）。"""
    last_changed = user["nickname_changed_at"]
    if not last_changed:
        return ""
    try:
        last_dt = datetime.fromisoformat(last_changed)
    except (TypeError, ValueError):
        return ""
    if (datetime.now() - last_dt).days < NICKNAME_CHANGE_INTERVAL_DAYS:
        return t(lang, "user_err_nickname_frequency")
    return ""


def _change_password(request, user, lang):
    """返回 None 表示成功；否则返回错误文案。"""
    old_password = request.form.get("old_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not verify_password(old_password, user["password"]):
        # 安全信号：拿着有效会话却给不出当前密码（会话被劫持/误操作都要能看见）
        logger.warning("Password change rejected (wrong current password): "
                       "user_id=%s ip=%s", user["id"], request.client_ip)
        return t(lang, "user_err_old_password")
    if new_password != confirm_password:
        return t(lang, "auth_err_password_mismatch")
    if len(new_password) < PASSWORD_MIN or len(new_password) > PASSWORD_MAX:
        return t(lang, "auth_err_password_range")
    update_user_password(user["id"], hash_password(new_password))
    return None
