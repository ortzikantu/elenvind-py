"""评论区渲染（评论列表 + 回复/删除/恢复表单）。

与 view_partials_navbar/footer 一样按局部视图模块组织：
评论区 UI 可复用，直接并入文章页视图会使其过于臃肿。
界面文案走 i18n（lang 由文章视图透传）。
"""
from .config import config
from .i18n import t
from .utils import escape_html, format_datetime
from .db_comment import get_comments_by_article

# 兜底默认值与 config.toml 顶层键保持一致（max_length / deleted_user_nickname）
DEFAULT_DELETED_NICKNAME = "Journeyed On"
DEFAULT_MAX_LENGTH = 1000


def render_comments(article_slug: str, user, reply_to: int = None, csrf_token: str = None,
                    lang: str = "en") -> str:
    """渲染整个评论区：评论列表 + 评论表单（仅已登录用户可见）。"""
    # 徽章默认 "BIG BOSS"；显式留空（""）则管理员也不渲染徽章
    admin_badge = str(config.get("admin_badge", "BIG BOSS")).strip()
    comments = get_comments_by_article(article_slug)
    total = len(comments)
    slug_esc = escape_html(article_slug)

    # 区块标题保留 #comments 锚点，评论发布后的重定向依赖该锚点
    title_line = escape_html(t(lang, "comment_comments", total=total))
    comments_html = f"<section class='comments' id='comments'><h2>{title_line}</h2>"

    if comments:
        # 把 SQL 查出的平铺行在内存中组装成树：顶层评论在前，后代按顺序排在其后
        top_comments = [c for c in comments if c["parent_id"] is None]
        for top in top_comments:
            comments_html += _render_single_comment(top, admin_badge, user=user,
                                                    csrf_token=csrf_token, lang=lang, is_top=True)
            descendants = _get_descendants(top["id"], comments)
            for desc in descendants:
                comments_html += _render_single_comment(desc, admin_badge, user=user,
                                                        csrf_token=csrf_token, lang=lang, is_top=False)
    else:
        comments_html += f"<p>{escape_html(t(lang, 'comment_none'))}</p>"

    comments_html += "</section>"

    # 评论/回复表单：仅对已登录用户展示
    if user:
        max_length = config.get("max_length", DEFAULT_MAX_LENGTH)
        reply_hint = ""
        reply_to_value = ""

        if reply_to:
            parent_comment = next((c for c in comments if c["id"] == reply_to), None)
            if parent_comment:
                deleted_nickname = config.get("deleted_user_nickname", DEFAULT_DELETED_NICKNAME)
                if parent_comment["user_deleted"]:
                    parent_nickname = f"{escape_html(deleted_nickname)} (#{parent_comment['user_id']})"
                else:
                    parent_nickname = f"{escape_html(parent_comment['nickname'])} (#{parent_comment['user_id']})"
                reply_text = escape_html(t(lang, "comment_replying", name=parent_nickname))
                reply_hint = f"<p>{reply_text}</p>"
                reply_to_value = str(reply_to)

        post_title = escape_html(t(lang, "comment_post_title"))
        placeholder = escape_html(t(lang, "comment_placeholder"))
        submit_text = escape_html(t(lang, "comment_submit"))
        comments_html += f"""
        <h3>{post_title}</h3>
        <form method='post' action='/article/{slug_esc}/comment'>
            <input type='hidden' name='csrf_token' value='{csrf_token or ""}'>
            <input type='hidden' name='reply_to' value='{reply_to_value}'>
            {reply_hint}
            <textarea name='content' rows='4' required maxlength='{max_length}' placeholder='{placeholder}'></textarea>
            <button type='submit'>{submit_text}</button>
        </form>
        """
    else:
        login_hint = escape_html(t(lang, "comment_login_hint"))
        comments_html += f"<p>{login_hint}</p>"

    return comments_html


def _get_descendants(parent_id: int, all_comments: list) -> list:
    """收集一条评论的完整回复子树（深度优先，按发表顺序）。"""
    descendants = []
    for comment in all_comments:
        if comment["parent_id"] == parent_id:
            descendants.append(comment)
            descendants.extend(_get_descendants(comment["id"], all_comments))
    return descendants


def _display_text(raw: str) -> str:
    """做 HTML 转义并保留换行（textarea 输入自带换行符）。"""
    text = escape_html(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return text


def _render_single_comment(comment, admin_badge, user=None, csrf_token=None,
                           lang="en", is_top=True) -> str:
    """渲染单条评论。软删除的评论对访客打码隐藏，
    对可将其恢复的管理员（id=1）显示删除线。"""
    is_admin = user and user["id"] == 1
    deleted_nickname = config.get("deleted_user_nickname", DEFAULT_DELETED_NICKNAME)

    # 账号注销后评论仍保留，但昵称以占位文案展示
    if comment["user_deleted"]:
        display_nickname = f"{escape_html(deleted_nickname)} (#{comment['user_id']})"
    else:
        display_nickname = f"{escape_html(comment['nickname'])} (#{comment['user_id']})"

    created = escape_html(format_datetime(comment["created_at"]))
    badge = (
        f'<span class="badge-admin">{escape_html(admin_badge)}</span>'
        if admin_badge and comment["user_id"] == 1 else ""
    )

    raw_content = comment["content"]
    if comment["is_deleted"] and not is_admin:
        # 用与原文等长的实心方块打码，避免泄露内容
        block_len = len(raw_content.replace("\r\n", "\n").replace("\r", "\n"))
        content_display = f'<span class="comment-content">{"█" * block_len}</span>'
    elif comment["is_deleted"] and is_admin:
        content_display = (
            f'<span class="comment-content is-deleted">{_display_text(raw_content)}</span>'
        )
    else:
        content_display = f'<span class="comment-content">{_display_text(raw_content)}</span>'

    # 回复正文带 “Reply to @昵称：” 前缀，指明被回复的父评论作者
    if comment["parent_id"]:
        if comment["parent_user_deleted"]:
            parent_nickname = f"{escape_html(deleted_nickname)} (#{comment['parent_user_id']})"
        elif comment["parent_nickname"] is None:
            parent_nickname = escape_html(t(lang, "article_unknown"))
        else:
            parent_nickname = f"{escape_html(comment['parent_nickname'])} (#{comment['parent_user_id']})"
        body_content = f'<span class="reply-to">{escape_html(t(lang, "comment_replying", name=parent_nickname))} </span>{content_display}'
    else:
        body_content = content_display

    div_class = "comment" if is_top else "comment comment-reply"
    slug_esc = escape_html(comment["article_slug"])

    delete_text = escape_html(t(lang, "comment_delete"))
    restore_text = escape_html(t(lang, "comment_restore"))
    reply_text = escape_html(t(lang, "comment_reply"))

    # 删除表单（作者或管理员）／恢复表单（仅管理员）
    actions_html = ""
    if user and csrf_token:
        is_owner = user["id"] == comment["user_id"]
        if not comment["is_deleted"]:
            if is_admin or is_owner:
                delete_form = f"""
                <form method='post' action='/article/{slug_esc}/comment/delete/{comment["id"]}' class='comment-form'>
                    <input type='hidden' name='csrf_token' value='{csrf_token}'>
                    <button type='submit' class='delete-link'>{delete_text}</button>
                </form>"""
                actions_html += delete_form
        else:
            if is_admin:
                restore_form = f"""
                <form method='post' action='/article/{slug_esc}/comment/restore/{comment["id"]}' class='comment-form'>
                    <input type='hidden' name='csrf_token' value='{csrf_token}'>
                    <button type='submit' class='restore-link'>{restore_text}</button>
                </form>"""
                actions_html += restore_form

    # 回复是普通 GET 链接，通过 ?reply_to= 预填回复目标
    reply_link = f'<a class="reply-link" href="/article/{slug_esc}?reply_to={comment["id"]}#comments">{reply_text}</a>'

    return f"""
        <div class="{div_class}">
            <div class="meta">
                <span>{display_nickname}{badge} · {created}</span>
                <div class="comment-actions">
                    {actions_html}{reply_link}
                </div>
            </div>
            <p class="comment-body">{body_content}</p>
        </div>
    """
