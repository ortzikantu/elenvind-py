"""评论区渲染（评论列表 + 回复/删除/恢复表单）。

与 view_partials_navbar/footer 一样按局部视图模块组织：
评论区 UI 可复用，直接并入文章页视图会使其过于臃肿。
界面文案走 i18n（lang 由文章视图透传）。

评论树（v2）：
- 先在内存里按 parent_id 分桶，再做一次迭代式 DFS 展开成"顶层评论 + 其全部后代"
  的线性序列。整体 O(n)，且**不使用递归**，因此既没有 O(n²) 的重复扫描，
  也不会因为恶意构造的超深楼中楼触发 RecursionError。
- 父评论不存在（历史孤儿数据、父行被物理清除）时，该评论按顶层评论展示，
  不会从页面上"消失"。
- 超过 max_comment_depth 的层级不再提供回复入口，与后端拒绝保持同一策略。
"""
from .config import config
from .i18n import t
from .security import is_admin, is_admin_id
from .utils import escape_html, format_datetime
from .db_comment import get_comments_by_article

# 兜底默认值与 config.toml 顶层键保持一致（max_length / deleted_user_nickname）
DEFAULT_DELETED_NICKNAME = "Journeyed On"
DEFAULT_MAX_LENGTH = 1000
DEFAULT_MAX_DEPTH = 32


def max_comment_depth() -> int:
    """当前配置允许的最大评论层级（顶层 = 1）。"""
    try:
        return max(1, int(config.get("max_comment_depth", DEFAULT_MAX_DEPTH)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_DEPTH


def _parent_key(comment):
    """父评论 id：顶层（含悬空父）统一返回整数 id 或 None。"""
    return comment["parent_id"]


def build_tree(comments):
    """把扁平评论行整理为 [(comment, depth), ...] 的展示序列（迭代式 DFS）。

    规则：
    - 以"parent_id 为 None"或"父评论不在本次结果集中"的评论作为根；
    - 每个节点的子节点按 (created_at, id) 升序（SQL 已排序，天然有序）；
    - 迭代推进显式栈，深度仅受数据约束，不受 Python 递归深度限制。
    """
    by_id = {comment["id"]: comment for comment in comments}
    children = {}
    roots = []
    for comment in comments:
        parent_id = _parent_key(comment)
        if parent_id is None or parent_id not in by_id:
            roots.append(comment)
        else:
            children.setdefault(parent_id, []).append(comment)

    ordered = []
    # stack 元素：(评论行, 深度)；逆序压栈以保证弹出顺序与发表顺序一致
    stack = [(comment, 1) for comment in reversed(roots)]
    while stack:
        comment, depth = stack.pop()
        ordered.append((comment, depth))
        descendants = children.get(comment["id"])
        if descendants:
            for child in reversed(descendants):
                stack.append((child, depth + 1))
    return ordered, by_id


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

    ordered, by_id = build_tree(comments)
    depth_limit = max_comment_depth()
    if ordered:
        for comment, depth in ordered:
            comments_html += _render_single_comment(
                comment, admin_badge, user=user, csrf_token=csrf_token, lang=lang,
                depth=depth, by_id=by_id, depth_limit=depth_limit)
    else:
        comments_html += f"<p>{escape_html(t(lang, 'comment_none'))}</p>"

    comments_html += "</section>"

    # 评论/回复表单：仅对已登录用户展示
    if user:
        max_length = config.get("max_length", DEFAULT_MAX_LENGTH)
        reply_hint = ""
        reply_to_value = ""

        if reply_to:
            parent_comment = by_id.get(reply_to)
            if parent_comment:
                reply_hint = f"<p>{_reply_hint_html(parent_comment, lang)}</p>"
                reply_to_value = str(reply_to)

        post_title = escape_html(t(lang, "comment_post_title"))
        placeholder = escape_html(t(lang, "comment_placeholder"))
        submit_text = escape_html(t(lang, "comment_submit"))
        comments_html += f"""
        <h3>{post_title}</h3>
        <form method='post' action='/article/{slug_esc}/comment'>
            <input type='hidden' name='csrf_token' value='{escape_html(csrf_token or "")}'>
            <input type='hidden' name='reply_to' value='{escape_html(reply_to_value)}'>
            {reply_hint}
            <textarea name='content' rows='4' required maxlength='{int(max_length)}' placeholder='{placeholder}'></textarea>
            <button type='submit'>{submit_text}</button>
        </form>
        """
    else:
        login_hint = escape_html(t(lang, "comment_login_hint"))
        comments_html += f"<p>{login_hint}</p>"

    return comments_html


def _display_text(raw: str) -> str:
    """做 HTML 转义并保留换行（textarea 输入自带换行符）。"""
    text = escape_html(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return text


def _author_label(comment, deleted_nickname: str, lang: str) -> str:
    """作者展示名原始文本："昵称 (#id)" 或注销占位文案。

    返回值**未转义**：它会作为 {name} 参数进入 i18n 文案，格式化后再统一转义
    （否则 & < 这类字符会被转义两次，页面上出现 &amp;amp; 字面量）。
    """
    if comment["user_deleted"]:
        return f"{deleted_nickname} (#{comment['user_id']})"
    nickname = comment["nickname"] or t(lang, "article_unknown")
    return f"{nickname} (#{comment['user_id']})"


def _reply_hint_html(parent_comment, lang: str) -> str:
    """回复表单上方的 "正在回复 @某人" 提示（此处完成唯一一次转义）。"""
    deleted_nickname = config.get("deleted_user_nickname", DEFAULT_DELETED_NICKNAME)
    return escape_html(t(lang, "comment_replying",
                         name=_author_label(parent_comment, deleted_nickname, lang)))


def _render_single_comment(comment, admin_badge, user=None, csrf_token=None, lang="en",
                           depth=1, by_id=None, depth_limit=DEFAULT_MAX_DEPTH) -> str:
    """渲染单条评论。软删除的评论对访客打码隐藏，对管理员显示删除线。"""
    is_admin_user = is_admin(user)
    deleted_nickname = config.get("deleted_user_nickname", DEFAULT_DELETED_NICKNAME)
    display_nickname = escape_html(_author_label(comment, deleted_nickname, lang))
    created = escape_html(format_datetime(comment["created_at"]))
    badge = (
        f'<span class="badge-admin">{escape_html(admin_badge)}</span>'
        if admin_badge and is_admin_id(comment["user_id"]) else ""
    )

    raw_content = comment["content"]
    if comment["is_deleted"] and not is_admin_user:
        # 用与原文等长的实心方块打码，避免泄露内容
        block_len = len(raw_content.replace("\r\n", "\n").replace("\r", "\n"))
        content_display = f'<span class="comment-content">{"█" * block_len}</span>'
    elif comment["is_deleted"] and is_admin_user:
        content_display = (
            f'<span class="comment-content is-deleted">{_display_text(raw_content)}</span>'
        )
    else:
        content_display = f'<span class="comment-content">{_display_text(raw_content)}</span>'

    # 回复正文带 “Reply to @昵称：” 前缀，指明被回复的父评论作者
    parent = by_id.get(comment["parent_id"]) if by_id else None
    if comment["parent_id"] and parent is not None:
        parent_label = _author_label(parent, deleted_nickname, lang)
        body_content = (
            f'<span class="reply-to">'
            f'{escape_html(t(lang, "comment_replying", name=parent_label))} '
            f'</span>{content_display}'
        )
    else:
        body_content = content_display

    div_class = "comment" if depth <= 1 else "comment comment-reply"
    slug_esc = escape_html(comment["article_slug"])

    delete_text = escape_html(t(lang, "comment_delete"))
    restore_text = escape_html(t(lang, "comment_restore"))
    reply_text = escape_html(t(lang, "comment_reply"))

    # 删除表单（作者或管理员）／恢复表单（仅管理员）
    actions_html = ""
    if user and csrf_token:
        csrf_esc = escape_html(csrf_token)
        is_owner = user["id"] == comment["user_id"]
        if not comment["is_deleted"]:
            if is_admin_user or is_owner:
                actions_html += f"""
                <form method='post' action='/article/{slug_esc}/comment/delete/{comment["id"]}' class='comment-form'>
                    <input type='hidden' name='csrf_token' value='{csrf_esc}'>
                    <button type='submit' class='delete-link'>{delete_text}</button>
                </form>"""
        else:
            if is_admin_user:
                actions_html += f"""
                <form method='post' action='/article/{slug_esc}/comment/restore/{comment["id"]}' class='comment-form'>
                    <input type='hidden' name='csrf_token' value='{csrf_esc}'>
                    <button type='submit' class='restore-link'>{restore_text}</button>
                </form>"""

    # 回复是普通 GET 链接，通过 ?reply_to= 预填回复目标；
    # 到达层级上限后不再展示回复入口（后端同样拒绝，避免"点了没反应"）
    if depth < depth_limit:
        reply_link = (f'<a class="reply-link" href="/article/{slug_esc}?reply_to={comment["id"]}#comments">'
                      f'{reply_text}</a>')
    else:
        reply_link = ""

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
