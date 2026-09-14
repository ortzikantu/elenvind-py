from .config import config
from .view_layout_base import render as layout
from .security import (
    hash_password,
    verify_csrf_token,
    PASSWORD_MIN,
    PASSWORD_MAX,
    NICKNAME_MAX,
    EMAIL_MAX,
)
from .db_user import create_user, get_user_by_email
from .utils import normalize_email, escape_html
from .i18n import t


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
    return t(lang, "auth_err_register_failed")


def render(request_method="GET", form_data=None, user=None, csrf_token=None,
           theme=None, path=None, lang="en"):
    title = config.get("title", "WHERE IS YOUR TITLE?")
    error = ""

    if request_method == "POST":
        submitted_token = form_data.get("csrf_token", "")
        if not verify_csrf_token(submitted_token, csrf_token):
            error = _input_error(lang, "csrf")
        else:
            nickname = form_data.get("nickname", "").strip()
            email = normalize_email(form_data.get("email", ""))
            password = form_data.get("password", "")
            confirm = form_data.get("confirm_password", "")

            if not nickname or not email or not password:
                error = _input_error(lang, "required")
            elif len(nickname) > NICKNAME_MAX:
                error = _input_error(lang, "invalid")
            elif len(email) > EMAIL_MAX or "@" not in email:
                error = _input_error(lang, "invalid")
            elif len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
                error = _input_error(lang, "password_range")
            elif password != confirm:
                error = _input_error(lang, "mismatch")
            elif get_user_by_email(email):
                # 模糊提示，防用户枚举
                error = _input_error(lang, "invalid")
            else:
                password_hash = hash_password(password)
                create_user(nickname, email, password_hash)
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
        <input type="hidden" name="csrf_token" value="{csrf_token or ''}">
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
