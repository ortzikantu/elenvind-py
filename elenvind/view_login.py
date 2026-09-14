from .config import config
from .view_layout_base import render as layout
from .security import verify_password, verify_csrf_token, PASSWORD_MAX, EMAIL_MAX
from .db_user import get_user_by_email
from .db_login import (
    record_login_attempt,
    count_email_failures,
    count_ip_failures,
    count_global_recent_failures,
    clear_login_attempts,
)
from .db_session import create_session, delete_user_sessions
from .utils import normalize_email, escape_html
from .i18n import t

# 限流阈值（三个维度独立生效，任一层命中即拒绝本次尝试）：
# - 邮箱维度（主防线）：防单账号定向爆破；NAT 用户不会因他人失败被长期误锁
MAX_EMAIL_ATTEMPTS = 5                # 单邮箱 24h 内失败上限
EMAIL_WINDOW_SECONDS = 24 * 3600      # 邮箱锁定窗口：24 小时
# - IP 维度（辅助防线）：短窗口 + 高阈值，兼顾防脚本轮询与共享出口的可用性
MAX_IP_ATTEMPTS = 20                  # 单 IP 15min 内失败上限
IP_WINDOW_SECONDS = 15 * 60           # IP 统计窗口：15 分钟
# - 全局维度（最后闸门）：缓解分布式（多 IP）爆破
GLOBAL_MAX_ATTEMPTS = 200             # 全局失败次数上限
GLOBAL_WINDOW_SECONDS = 15 * 60       # 全局统计窗口：15 分钟


def _input_error(lang: str, reason: str) -> str:
    """把输入校验失败原因映射为模糊的通用错误文案（防用户枚举）。"""
    if reason in ("empty", "too_long", "bad_email"):
        return t(lang, "auth_err_creds")
    if reason == "global_lock":
        return t(lang, "auth_err_global")
    if reason == "email_lock":
        return t(lang, "auth_err_email_lock")
    if reason == "ip_lock":
        return t(lang, "auth_err_ip_lock")
    return t(lang, "auth_err_creds")


def render(request_method="GET", form_data=None, user=None, client_ip=None, csrf_token=None,
           theme=None, path=None, lang="en"):
    title = config.get("title", "WHERE IS YOUR TITLE?")
    error = ""

    if request_method == "POST":
        submitted_token = form_data.get("csrf_token", "")
        if not verify_csrf_token(submitted_token, csrf_token):
            error = t(lang, "auth_err_csrf")
        else:
            email = normalize_email(form_data.get("email", ""))
            password = form_data.get("password", "")
            ip = client_ip or "unknown"

            if len(email) > EMAIL_MAX or not email:
                error = _input_error(lang, "bad_email")
            elif len(password) > PASSWORD_MAX:
                error = _input_error(lang, "too_long")
            elif count_global_recent_failures(GLOBAL_WINDOW_SECONDS) >= GLOBAL_MAX_ATTEMPTS:
                error = _input_error(lang, "global_lock")
            elif count_email_failures(email, EMAIL_WINDOW_SECONDS) >= MAX_EMAIL_ATTEMPTS:
                error = _input_error(lang, "email_lock")
            elif count_ip_failures(ip, IP_WINDOW_SECONDS) >= MAX_IP_ATTEMPTS:
                error = _input_error(lang, "ip_lock")
            else:
                user_db = get_user_by_email(email)
                if user_db and verify_password(password, user_db["password"]):
                    # 清除旧会话（防会话固定）、清除失败记录
                    delete_user_sessions(user_db["id"])
                    clear_login_attempts(email)
                    record_login_attempt(email, ip, success=True)
                    token = create_session(user_db["id"])
                    return ("redirect", "/", token)
                else:
                    record_login_attempt(email, ip, success=False)
                    error = _input_error(lang, "bad_email")

    title_text = t(lang, "auth_login_title")
    email_label = escape_html(t(lang, "auth_email"))
    password_label = escape_html(t(lang, "auth_password"))
    submit_text = escape_html(t(lang, "auth_signin_btn"))
    register_prompt = escape_html(t(lang, "auth_register_prompt"))
    register_link = escape_html(t(lang, "auth_register_link"))
    msg_html = f'<p class="form-msg error">{escape_html(error)}</p>' if error else ""

    content = f"""
    <h1>{escape_html(title_text)}</h1>
    {msg_html}
    <form method="post" action="/login" class="field-grid">
        <input type="hidden" name="csrf_token" value="{csrf_token or ''}">
        <div class="field-row">
            <label class="field-label" for="login-email">{email_label}</label>
            <input type="email" id="login-email" name="email" maxlength="{EMAIL_MAX}" autocomplete="email" required>
        </div>
        <div class="field-row">
            <label class="field-label" for="login-password">{password_label}</label>
            <input type="password" id="login-password" name="password" maxlength="{PASSWORD_MAX}" autocomplete="current-password" required>
        </div>
        <div class="field-action"><button type="submit">{submit_text}</button></div>
    </form>
    <p>{register_prompt} <a href="/register">{register_link}</a></p>
    """
    return layout(title, content, user=user, theme=theme, path=path, lang=lang)
