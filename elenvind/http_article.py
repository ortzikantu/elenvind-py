"""文章页 GET 与评论相关 POST 的请求处理（由 http.py 分发调用）

CSRF 已在分发器统一校验（http._route_post），本模块只处理业务规则：
- 登录校验、文章存在性校验、回复目标合法性、层级上限、发布限流（原子）。
"""
import logging
from datetime import datetime

from .http_base import html_response, plain_response, redirect_response
from .db_comment import get_comment_by_id, soft_delete_comment, restore_comment
from .db_comment_rate import try_post_comment
from .articles import get_article_by_slug
from .config import config
from .security import admin_id
from . import view_article, view_404
from .view_partials_comment import max_comment_depth

logger = logging.getLogger(__name__)

# 评论发布限流默认值（可在 config.toml [comment_limits] 覆盖）
DEFAULT_COMMENT_LIMITS = {
    "max_per_user": 5,
    "max_per_ip": 10,
    "window_seconds": 60,
}
DEFAULT_MAX_COMMENTS_PER_ARTICLE = 1000


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _comment_limits() -> dict:
    """读取 [comment_limits] 配置，缺项回退默认值。"""
    raw = config.get("comment_limits") or {}
    limits = dict(DEFAULT_COMMENT_LIMITS)
    if isinstance(raw, dict):
        for key in limits:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


def _max_comments_per_article() -> int:
    value = config.get("max_comments_per_article", DEFAULT_MAX_COMMENTS_PER_ARTICLE)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_COMMENTS_PER_ARTICLE


def _error_page(ctx, message: str, status: int):
    """用文章页样式渲染一条错误提示（比裸文本更友好，且仍带完整布局）。"""
    body = view_article.render_error(message, user=ctx.user, theme=ctx.theme,
                                     path=ctx.path, lang=ctx.lang)
    return html_response(body, status=status)


def article_get(ctx, slug: str):
    """GET /article/{slug}（?reply_to=N 可选）"""
    if not slug or get_article_by_slug(slug) is None:
        return html_response(view_404.render(user=ctx.user, theme=ctx.theme, path=ctx.path, lang=ctx.lang),
                             status=404)
    reply_to = _safe_int(ctx.query.get("reply_to", [None])[0])
    # 登录用户页面含评论/操作表单，需要一致的 CSRF 令牌；匿名页无表单不生成 Cookie
    csrf_token = ctx.ensure_csrf() if ctx.user else None
    html = view_article.render(slug=slug, user=ctx.user, reply_to=reply_to, csrf_token=csrf_token,
                               theme=ctx.theme, path=ctx.path, lang=ctx.lang)
    return html_response(html)


def comment_create_post(ctx, slug: str):
    """发布评论：POST /article/{slug}/comment"""
    if not ctx.user:
        return plain_response("Forbidden", 403)
    if get_article_by_slug(slug) is None:
        # 拒绝为不存在的文章写入孤儿评论
        return plain_response("Article not found", 404)

    content_text = ctx.form.get("content", "").strip()
    max_len = config.get("max_length", 1000)
    if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len <= 0:
        max_len = 1000
    if not content_text:
        return _error_page(ctx, "Content cannot be empty", 400)
    if len(content_text) > max_len:
        return _error_page(ctx, "Content too long", 400)

    # 解析回复目标
    reply_to = None
    reply_to_str = ctx.form.get("reply_to", "")
    if reply_to_str:
        reply_to = _safe_int(reply_to_str)
        if reply_to is None:
            return _error_page(ctx, "Invalid reply target", 400)
        # 验证父评论存在、属于同一篇文章且未被软删除
        parent_comment = get_comment_by_id(reply_to)
        if not parent_comment or parent_comment["article_slug"] != slug:
            return _error_page(ctx, "Invalid reply target", 400)
        if parent_comment["is_deleted"]:
            return _error_page(ctx, "Cannot reply to a deleted comment", 400)
        # 层级上限：父评论深度 + 1 不得超过 max_comment_depth
        if _comment_depth(parent_comment) + 1 > max_comment_depth():
            return _error_page(ctx, "Reply depth limit reached", 400)

    limits = _comment_limits()
    outcome = try_post_comment(
        slug, ctx.user["id"], ctx.client_ip, content_text,
        parent_id=reply_to,
        max_per_user=limits["max_per_user"],
        max_per_ip=limits["max_per_ip"],
        window_seconds=limits["window_seconds"],
        max_per_article=_max_comments_per_article(),
        created_at=datetime.now().isoformat(),
    )
    if outcome == "rate_user":
        logger.warning("Comment rate limit hit (user): user_id=%s ip=%s slug=%s",
                       ctx.user["id"], ctx.client_ip, slug)
        return _error_page(ctx, "Too many comments. Please slow down.", 429)
    if outcome == "rate_ip":
        logger.warning("Comment rate limit hit (ip): user_id=%s ip=%s slug=%s",
                       ctx.user["id"], ctx.client_ip, slug)
        return _error_page(ctx, "Too many comments from this address. Please slow down.", 429)
    if outcome == "too_many":
        logger.warning("Comment count limit hit: slug=%s limit=%s",
                       slug, _max_comments_per_article())
        return _error_page(ctx, "This article has reached the comment limit.", 429)

    return redirect_response(f"/article/{slug}#comments")


def _comment_depth(comment) -> int:
    """沿 parent_id 链回溯计算层级（顶层 = 1）。

    迭代实现 + 访问集合：即使数据被外部改坏成环也不会死循环；
    达到配置上限即停止回溯，因此最坏代价是 O(max_comment_depth)。
    """
    depth = 1
    parent_id = comment["parent_id"]
    seen = {comment["id"]}
    limit = max_comment_depth() + 1
    while parent_id is not None and depth < limit:
        if parent_id in seen:
            break                      # 数据成环：按当前深度处理
        seen.add(parent_id)
        parent = get_comment_by_id(parent_id)
        if parent is None:
            break                      # 悬空父评论：视为顶层之后的第一层
        depth += 1
        parent_id = parent["parent_id"]
    return depth


def _comment_authorize(ctx, slug: str, raw_id: str, admin_only: bool = False):
    """删除/恢复前的公共校验。

    任一步失败即返回 (错误响应, None)；全部通过返回 (None, 评论对象)。
    """
    comment_id = _safe_int(raw_id)
    if comment_id is None:
        return plain_response("Invalid comment", 400), None
    comment = get_comment_by_id(comment_id)
    if not comment:
        return plain_response("Comment not found", 404), None
    if comment["article_slug"] != slug:
        return plain_response("Invalid comment", 400), None
    if not ctx.user:
        return plain_response("Forbidden", 403), None
    owner = admin_id()
    is_admin_user = owner is not None and ctx.user["id"] == owner
    if admin_only:
        if not is_admin_user:
            return plain_response("Forbidden", 403), None
    elif not is_admin_user and ctx.user["id"] != comment["user_id"]:
        return plain_response("Forbidden", 403), None
    return None, comment


def comment_delete_post(ctx, slug: str, raw_id: str):
    """POST /article/{slug}/comment/delete/{id}（本人或管理员）"""
    error, comment = _comment_authorize(ctx, slug, raw_id)
    if error is not None:
        return error
    if not comment["is_deleted"]:
        soft_delete_comment(comment["id"])
    return redirect_response(f"/article/{slug}#comments")


def comment_restore_post(ctx, slug: str, raw_id: str):
    """POST /article/{slug}/comment/restore/{id}（仅管理员）"""
    error, comment = _comment_authorize(ctx, slug, raw_id, admin_only=True)
    if error is not None:
        return error
    if comment["is_deleted"]:
        restore_comment(comment["id"])
    return redirect_response(f"/article/{slug}#comments")
