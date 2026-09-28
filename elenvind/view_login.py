import logging

from .config import config
from .view_layout_base import render as layout
from .security import (
    hash_password,
    password_needs_rehash,
    verify_password,
    PASSWORD_MAX,
    EMAIL_MAX,
)
from .db_user import get_user_by_email, update_user_password
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

logger = logging.getLogger(__name__)

# 限流阈值默认值（config.toml [login_limits] 可覆盖），三个维度独立生效，
# 任一层命中即拒绝本次尝试：
# - 邮箱维度（主防线）：防单账号定向爆破；NAT 用户不会因他人失败被长期误锁
# - IP 维度（辅助防线）：短窗口 + 高阈值，兼顾防脚本轮询与共享出口的可用性
# - 全局维度（最后闸门）：缓解分布式（多 IP）爆破
DEFAULT_LOGIN_LIMITS = {
    "max_email_failures": 5,
    "email_window_seconds": 24 * 3600,
    "max_ip_failures": 20,
    "ip_window_seconds": 15 * 60,
    "max_global_failures": 200,
    "global_window_seconds": 15 * 60,
}

# 用户不存在时用于"陪跑"校验的哑哈希：使"账号不存在"与"密码错误"两条路径
# 的计算量接近，降低计时侧信道带来的账号枚举风险。
# 该哈希对应一个不会有人使用的随机口令，仅用于消耗等量 CPU。
_DUMMY_PASSWORD_HASH = hash_password("elenvind-dummy-password-for-timing")


def login_limits() -> dict:
    """读取 [login_limits] 配置，缺项回退默认值。"""
    raw = config.get("login_limits") or {}
    limits = dict(DEFAULT_LOGIN_LIMITS)
    if isinstance(raw, dict):
        for key in limits:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


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
           csrf_ok=True, theme=None, path=None, lang="en"):
    form_data = form_data or {}
    title = config.get("title", "WHERE IS YOUR TITLE?")
    error = ""

    if request_method == "POST":
        # CSRF 由分发器统一校验；这里只做业务分支
        email = normalize_email(form_data.get("email", ""))
        password = form_data.get("password", "")
        ip = client_ip or "unknown"
        limits = login_limits()

        if not csrf_ok:
            error = t(lang, "auth_err_csrf")
        elif len(email) > EMAIL_MAX or not email:
            error = _input_error(lang, "bad_email")
        elif len(password) > PASSWORD_MAX:
            error = _input_error(lang, "too_long")
        elif count_global_recent_failures(limits["global_window_seconds"]) >= limits["max_global_failures"]:
            error = _input_error(lang, "global_lock")
        elif count_email_failures(email, limits["email_window_seconds"]) >= limits["max_email_failures"]:
            error = _input_error(lang, "email_lock")
        elif count_ip_failures(ip, limits["ip_window_seconds"]) >= limits["max_ip_failures"]:
            error = _input_error(lang, "ip_lock")
        else:
            user_db = get_user_by_email(email)
            stored_hash = user_db["password"] if user_db else _DUMMY_PASSWORD_HASH
            # 无论账号是否存在都执行一次密码校验（等量计算，弱化计时枚举）
            password_ok = verify_password(password, stored_hash)
            if user_db and password_ok:
                # 清除旧会话（防会话固定）、清除失败记录
                delete_user_sessions(user_db["id"])
                clear_login_attempts(email)
                record_login_attempt(email, ip, success=True)
                if password_needs_rehash(stored_hash):
                    # 渐进式 rehash：老格式/低代价哈希在下次成功登录时静默升级。
                    # 升级失败不影响本次登录（下次登录会再试），只记日志。
                    try:
                        update_user_password(user_db["id"], hash_password(password))
                    except Exception:
                        logger.exception("Password rehash failed for user_id=%s", user_db["id"])
                token = create_session(user_db["id"])
                return ("redirect", "/", token)
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
        <input type="hidden" name="csrf_token" value="{escape_html(csrf_token or '')}">
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
