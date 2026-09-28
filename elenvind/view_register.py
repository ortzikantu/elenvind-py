"""注册页渲染与注册业务规则。

安全要点：
- 公开注册可用 config.toml 顶层 registration_enabled = false 整体关闭；
- 注册受 IP 维度限流（config.toml [register_limits]，默认 1 小时 5 次）；
- 邮箱查重不做 SELECT 预检查：直接 INSERT，由 user.email 的 UNIQUE 约束做最终
  权威判定，捕获 sqlite3.IntegrityError 后返回"注册失败"提示而不是 500，
  从而天然免疫并发注册竞态（TOCTOU）。
- CSRF 已由分发器统一校验（csrf_ok 参数）。
"""
import logging
import sqlite3

from .config import config
from .view_layout_base import render as layout
from .security import (
    hash_password,
    PASSWORD_MIN,
    PASSWORD_MAX,
    NICKNAME_MAX,
    EMAIL_MAX,
)
from .db_user import create_user, get_user_by_email
from .db_register import try_register_attempt
from .utils import normalize_email, escape_html
from .i18n import t

logger = logging.getLogger(__name__)

DEFAULT_REGISTER_LIMITS = {"max_per_ip": 5, "window_seconds": 3600}


def registration_enabled() -> bool:
    """是否开放公开注册（默认开启）。"""
    return bool(config.get("registration_enabled", True))


def register_limits() -> dict:
    """读取 [register_limits] 配置，缺项回退默认值。"""
    raw = config.get("register_limits") or {}
    limits = dict(DEFAULT_REGISTER_LIMITS)
    if isinstance(raw, dict):
        for key in limits:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


def _input_error(lang: str, reason: str) -> str:
    """把校验失败原因映射为提示文案；注册失败一律模糊处理（防用户枚举）。"""
    if reason == "required":
        return t(lang, "auth_err_required")
    if reason == "invalid":
        return t(lang, "auth_err_register_failed")
    if reason == "password_range":
        return t(lang, "auth_err_password_range")
    if reason == "mismatch":
        return t(lang, "auth_err_password_mismatch")
    if reason == "csrf":
        return t(lang, "auth_err_csrf")
    if reason == "disabled":
        return t(lang, "auth_err_register_disabled")
    if reason == "rate":
        return t(lang, "auth_err_register_rate")
    return t(lang, "auth_err_register_failed")


def _validate(form_data):
    """校验注册表单，返回 (错误原因 或 None, 规范化后的字段)。"""
    nickname = form_data.get("nickname", "").strip()
    email = normalize_email(form_data.get("email", ""))
    password = form_data.get("password", "")
    confirm = form_data.get("confirm_password", "")

    if not nickname or not email or not password:
        return "required", (nickname, email, password)
    if len(nickname) > NICKNAME_MAX:
        return "invalid", (nickname, email, password)
    if len(email) > EMAIL_MAX or "@" not in email:
        return "invalid", (nickname, email, password)
    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        return "password_range", (nickname, email, password)
    if password != confirm:
        return "mismatch", (nickname, email, password)
    return None, (nickname, email, password)


def render(request_method="GET", form_data=None, user=None, csrf_token=None,
           client_ip=None, csrf_ok=True, theme=None, path=None, lang="en"):
    form_data = form_data or {}
    title = config.get("title", "WHERE IS YOUR TITLE?")
    error = ""

    if request_method == "POST":
        if not csrf_ok:
            error = _input_error(lang, "csrf")
        elif not registration_enabled():
            error = _input_error(lang, "disabled")
        else:
            reason, fields = _validate(form_data)
            nickname, email, password = fields
            if reason:
                error = _input_error(lang, reason)
            else:
                limits = register_limits()
                if not try_register_attempt(client_ip or "unknown",
                                            max_per_ip=limits["max_per_ip"],
                                            window_seconds=limits["window_seconds"]):
                    logger.warning("Registration rate limit hit: ip=%s", client_ip)
                    error = _input_error(lang, "rate")
                else:
                    # 友好提示用查重（非权威）；真正的唯一性由 UNIQUE 约束保证
                    if get_user_by_email(email):
                        error = _input_error(lang, "invalid")
                    else:
                        try:
                            create_user(nickname, email, hash_password(password))
                        except sqlite3.IntegrityError:
                            # 并发注册撞上同一邮箱：UNIQUE 约束拒绝了写入，
                            # 对用户而言就是"该邮箱已被使用"，绝不是 500
                            error = _input_error(lang, "invalid")
                        else:
                            return ("redirect", "/login", None)

    title_text = t(lang, "auth_register_title")
    nickname_label = escape_html(t(lang, "auth_nickname"))
    email_label = escape_html(t(lang, "auth_email"))
    password_label = escape_html(t(lang, "auth_password"))
    confirm_label = escape_html(t(lang, "auth_confirm_password"))
    submit_text = escape_html(t(lang, "auth_signup_btn"))
    login_prompt = escape_html(t(lang, "auth_login_prompt"))
    login_link = escape_html(t(lang, "auth_login_link"))
    msg_html = f'<p class="form-msg error">{escape_html(error)}</p>' if error else ""

    content = f"""
    <h1>{escape_html(title_text)}</h1>
    {msg_html}
    <form method="post" action="/register" class="field-grid">
        <input type="hidden" name="csrf_token" value="{escape_html(csrf_token or '')}">
        <div class="field-row">
            <label class="field-label" for="reg-nickname">{nickname_label}</label>
            <input type="text" id="reg-nickname" name="nickname" maxlength="{NICKNAME_MAX}" autocomplete="nickname" required>
        </div>
        <div class="field-row">
            <label class="field-label" for="reg-email">{email_label}</label>
            <input type="email" id="reg-email" name="email" maxlength="{EMAIL_MAX}" autocomplete="email" required>
        </div>
        <div class="field-row">
            <label class="field-label" for="reg-password">{password_label}</label>
            <input type="password" id="reg-password" name="password" minlength="{PASSWORD_MIN}" maxlength="{PASSWORD_MAX}" autocomplete="new-password" required>
        </div>
        <div class="field-row">
            <label class="field-label" for="reg-confirm">{confirm_label}</label>
            <input type="password" id="reg-confirm" name="confirm_password" minlength="{PASSWORD_MIN}" maxlength="{PASSWORD_MAX}" autocomplete="new-password" required>
        </div>
        <div class="field-action"><button type="submit">{submit_text}</button></div>
    </form>
    <p>{login_prompt} <a href="/login">{login_link}</a></p>
    """
    return layout(title, content, user=user, theme=theme, path=path, lang=lang)
