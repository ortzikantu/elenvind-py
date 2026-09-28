"""个人中心页面：资料展示、资料修改、密码修改、删除账号。

结构约定（与 /login /register 保持同一套表单排版）：
- 顶部 Profile 区块展开，下方三个功能区块为可折叠 details 面板（零 JS）；
- 字段一律用 .field-row（标签列右对齐、控件列左对齐，窄屏自动堆叠）；
- 提示条用 .form-msg error/success，不使用内联样式；
- 删除账号区块整体用 .danger-zone 红虚线框警示；
- 界面文案走 i18n（lang 由请求层透传）。
"""
import sqlite3
from datetime import datetime

from .config import config
from .view_layout_base import render as layout
from .utils import escape_html, format_datetime, normalize_email
from .security import (
    hash_password,
    verify_password,
    PASSWORD_MIN,
    PASSWORD_MAX,
    NICKNAME_MAX,
    EMAIL_MAX,
)
from .db_user import (
    get_user_by_email,
    update_user_nickname,
    update_user_email,
    update_user_password,
    delete_user,
)
from .db_session import delete_user_sessions
from .i18n import t

NICKNAME_CHANGE_INTERVAL_DAYS = 365


def _field_row(label_html: str, control_html: str) -> str:
    """拼一个字段行：左侧标签（可含 hint），右侧控件/取值。"""
    return f"""            <div class="field-row">
                <div class="field-label">{label_html}</div>
                {control_html}
            </div>"""


def _label(text: str, for_id: str = "", hint: str = "") -> str:
    """标签（带可选 for 与说明小字 hint；text/hint 均按已翻译文本传入）。"""
    label_tag = f'<label for="{for_id}">{text}</label>' if for_id else text
    hint_html = f'<span class="field-hint">{hint}</span>' if hint else ""
    return label_tag + hint_html


def render(request_method="GET", form_data=None, user=None, csrf_token=None,
           csrf_ok=True, theme=None, path=None, lang="en"):
    form_data = form_data or {}
    title = config.get("title", "WHERE IS YOUR TITLE?")
    error = ""
    success = ""

    if not user:
        title_text = t(lang, "user_title")
        anon_desc = t(lang, "user_anon_desc")
        sign_in_text = t(lang, "nav_sign_in")
        content = f"""
        <h1>{escape_html(title_text)}</h1>
        <p>{escape_html(anon_desc)}</p>
        <p><a href="/login">{escape_html(sign_in_text)}</a></p>
        """
        return layout(title, content, user=user, theme=theme, path=path, lang=lang)

    if request_method == "POST":
        # CSRF 由分发器统一校验；这里只做业务分支
        if not csrf_ok:
            error = t(lang, "auth_err_csrf")
        else:
            action = form_data.get("action", "")
            if action == "update_profile":
                new_nickname = form_data.get("nickname", "").strip()
                new_email = normalize_email(form_data.get("email", ""))
                current_password = form_data.get("current_password", "")

                if not new_nickname or not new_email:
                    error = t(lang, "user_err_empty")
                elif len(new_nickname) > NICKNAME_MAX:
                    error = t(lang, "user_err_nickname_long")
                elif len(new_email) > EMAIL_MAX or "@" not in new_email:
                    error = t(lang, "user_err_email_invalid")
                else:
                    email_changed = new_email != normalize_email(user["email"])
                    if email_changed and not verify_password(current_password, user["password"]):
                        error = t(lang, "user_err_password_for_email")
                    else:
                        # 检查邮箱是否被其他用户占用（大小写不敏感）
                        existing = get_user_by_email(new_email)
                        if existing and existing["id"] != user["id"]:
                            error = t(lang, "user_err_email_used")
                        else:
                            # 昵称更改频率限制（仅当昵称真的变化时计时）
                            nickname_changed = new_nickname != user["nickname"]
                            if nickname_changed:
                                last_changed = user["nickname_changed_at"]
                                if last_changed:
                                    try:
                                        last_dt = datetime.fromisoformat(last_changed)
                                    except (TypeError, ValueError):
                                        # 历史数据格式异常：视为无限制，允许更改
                                        last_dt = None
                                    if last_dt is not None and (datetime.now() - last_dt).days < NICKNAME_CHANGE_INTERVAL_DAYS:
                                        error = t(lang, "user_err_nickname_frequency")
                            if not error:
                                try:
                                    if nickname_changed:
                                        update_user_nickname(user["id"], new_nickname)
                                    if email_changed:
                                        update_user_email(user["id"], new_email)
                                except sqlite3.IntegrityError:
                                    # 并发下邮箱被他人抢先占用：UNIQUE 约束给出最终判定
                                    error = t(lang, "user_err_email_used")
                                else:
                                    success = t(lang, "user_ok_profile")
                                    user = dict(user)
                                    user["nickname"] = new_nickname
                                    user["email"] = new_email
                                    if nickname_changed:
                                        user["nickname_changed_at"] = datetime.now().isoformat()
            elif action == "change_password":
                old_password = form_data.get("old_password", "")
                new_password = form_data.get("new_password", "")
                confirm_password = form_data.get("confirm_password", "")
                if not verify_password(old_password, user["password"]):
                    error = t(lang, "user_err_old_password")
                elif new_password != confirm_password:
                    error = t(lang, "auth_err_password_mismatch")
                elif len(new_password) < PASSWORD_MIN or len(new_password) > PASSWORD_MAX:
                    error = t(lang, "auth_err_password_range")
                else:
                    new_hash = hash_password(new_password)
                    update_user_password(user["id"], new_hash)
                    # 返回特殊标记，通知调用方清除所有会话并跳转登录页
                    return ("password_changed",)
            elif action == "delete_account":
                # 要求输入当前密码确认
                password_confirm = form_data.get("password_confirm", "")
                if not verify_password(password_confirm, user["password"]):
                    error = t(lang, "user_err_password_incorrect")
                else:
                    # 逻辑删除用户并清除其所有会话
                    delete_user(user["id"])
                    delete_user_sessions(user["id"])
                    # 返回特殊标记，通知调用方清除 Cookie 并跳转首页
                    return ("redirect_logout",)
            else:
                error = t(lang, "user_err_unknown_action")

    # 消息条（成功/失败二选一）
    if success:
        msg_html = f'<p class="form-msg success">{escape_html(success)}</p>'
    elif error:
        msg_html = f'<p class="form-msg error">{escape_html(error)}</p>'
    else:
        msg_html = ""

    csrf_html = f'<input type="hidden" name="csrf_token" value="{escape_html(csrf_token or "")}">'

    # ---- 各区块标题与字段文案（按语言取词） ----
    page_title = escape_html(t(lang, "user_title"))
    s_profile = escape_html(t(lang, "user_section_profile"))
    s_edit = escape_html(t(lang, "user_section_edit"))
    s_password = escape_html(t(lang, "user_section_password"))
    s_delete = escape_html(t(lang, "user_section_delete"))
    label_id = escape_html(t(lang, "user_id"))
    label_nickname = escape_html(t(lang, "user_nickname"))
    label_email = escape_html(t(lang, "user_email"))
    label_registered = escape_html(t(lang, "user_registered_at"))
    logout_text = escape_html(t(lang, "user_logout"))
    hint_email = escape_html(t(lang, "user_password_hint_email"))
    hint_len = escape_html(t(lang, "user_password_hint_len"))
    btn_update = escape_html(t(lang, "user_update_btn"))
    btn_change = escape_html(t(lang, "user_change_btn"))
    btn_delete = escape_html(t(lang, "user_delete_btn"))
    delete_note = escape_html(t(lang, "user_delete_note"))
    label_current_pw = escape_html(t(lang, "user_current_password"))
    label_new_pw = escape_html(t(lang, "user_new_password"))
    label_confirm_pw = escape_html(t(lang, "user_confirm_new_password"))
    label_delete_pw = escape_html(t(lang, "user_delete_password"))
    label_pw_edit = escape_html(t(lang, "auth_password"))

    # 账号信息展示行
    profile_rows = "".join([
        _field_row(f"<span>{label_id}</span>", f'<div class="field-value">{user["id"]}</div>'),
        _field_row(f"<span>{label_nickname}</span>",
                   f'<div class="field-value">{escape_html(user["nickname"])}</div>'),
        _field_row(f"<span>{label_email}</span>",
                   f'<div class="field-value">{escape_html(user["email"])}</div>'),
        _field_row(f"<span>{label_registered}</span>",
                   f'<div class="field-value">{escape_html(format_datetime(user["created_at"]))}</div>'),
    ])

    content = f"""
    <h1>{page_title}</h1>
    {msg_html}
    <section>
        <h2>{s_profile}</h2>
        <div class="field-grid">
        {profile_rows}
        </div>
        <form method="post" action="/logout" class="field-action">
            {csrf_html}
            <button type="submit" class="text-link-button">{logout_text}</button>
        </form>
    </section>

    <details class="panel">
        <summary><h2>{s_edit}</h2></summary>
        <form method="post" action="/user" class="field-grid">
            {csrf_html}
            <input type="hidden" name="action" value="update_profile">
            {_field_row(
                _label(label_nickname, "profile-nickname"),
                f'<input type="text" id="profile-nickname" name="nickname" '
                f'value="{escape_html(user["nickname"])}" maxlength="{NICKNAME_MAX}" '
                f'autocomplete="nickname" required>')}
            {_field_row(
                _label(label_email, "profile-email"),
                f'<input type="email" id="profile-email" name="email" '
                f'value="{escape_html(user["email"])}" maxlength="{EMAIL_MAX}" '
                f'autocomplete="email" required>')}
            {_field_row(
                _label(label_pw_edit, "profile-password", hint_email),
                f'<input type="password" id="profile-password" name="current_password" '
                f'maxlength="{PASSWORD_MAX}" autocomplete="current-password">')}
            <div class="field-action"><button type="submit">{btn_update}</button></div>
        </form>
    </details>

    <details class="panel">
        <summary><h2>{s_password}</h2></summary>
        <form method="post" action="/user" class="field-grid">
            {csrf_html}
            <input type="hidden" name="action" value="change_password">
            {_field_row(
                _label(label_current_pw, "old-password"),
                f'<input type="password" id="old-password" name="old_password" '
                f'maxlength="{PASSWORD_MAX}" autocomplete="current-password" required>')}
            {_field_row(
                _label(label_new_pw, "new-password", hint_len),
                f'<input type="password" id="new-password" name="new_password" '
                f'minlength="{PASSWORD_MIN}" maxlength="{PASSWORD_MAX}" '
                f'autocomplete="new-password" required>')}
            {_field_row(
                _label(label_confirm_pw, "confirm-password"),
                f'<input type="password" id="confirm-password" name="confirm_password" '
                f'minlength="{PASSWORD_MIN}" maxlength="{PASSWORD_MAX}" '
                f'autocomplete="new-password" required>')}
            <div class="field-action"><button type="submit">{btn_change}</button></div>
        </form>
    </details>

    <details class="panel danger-zone">
        <summary><h2>{s_delete}</h2></summary>
        <p class="danger-note">{delete_note}</p>
        <form method="post" action="/user" class="field-grid">
            {csrf_html}
            <input type="hidden" name="action" value="delete_account">
            {_field_row(
                _label(label_delete_pw, "delete-confirm-password"),
                '<input type="password" id="delete-confirm-password" name="password_confirm" '
                'autocomplete="current-password" required>')}
            <div class="field-action"><button type="submit" class="btn-danger">{btn_delete}</button></div>
        </form>
    </details>
    """
    return layout(title, content, user=user, theme=theme, path=path, lang=lang)
