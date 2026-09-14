"""文章页 GET 与评论相关 POST 的请求处理（由 http.py 分发调用）"""
import logging
from .http_base import html_response, plain_response, redirect_response
from .security import verify_csrf_token
from .db_comment import get_comment_by_id, create_comment, soft_delete_comment, restore_comment
from .db_comment_rate import (
    record_comment_attempt,
    count_user_recent,
    count_ip_recent,
)
from .articles import get_article_by_slug
from .config import config
from . import view_article, view_404

logger = logging.getLogger(__name__)

# 评论发布限流：60 秒窗口内 单用户最多 5 条 / 单 IP 最多 10 条
COMMENT_WINDOW_SECONDS = 60
COMMENT_MAX_PER_USER = 5
COMMENT_MAX_PER_IP = 10

def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _form_csrf_ok(ctx) -> bool:
    return verify_csrf_token(ctx.form.get("csrf_token", ""), ctx.csrf())


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
    if not _form_csrf_ok(ctx):
        return plain_response("Invalid CSRF token", 400)
    if get_article_by_slug(slug) is None:
        # 拒绝为不存在的文章写入孤儿评论
        return plain_response("Article not found", 404)

    # 评论发布限流（通过登录与 CSRF 校验后才计数，避免记录无效噪音）
    record_comment_attempt(ctx.user["id"], ctx.client_ip)
    if count_user_recent(ctx.user["id"], COMMENT_WINDOW_SECONDS) > COMMENT_MAX_PER_USER:
        logger.warning(
            "Comment rate limit hit (user): user_id=%s ip=%s slug=%s",
            ctx.user["id"], ctx.client_ip, slug,
        )
        return plain_response("Too many comments. Please slow down.", 429)
    if count_ip_recent(ctx.client_ip, COMMENT_WINDOW_SECONDS) > COMMENT_MAX_PER_IP:
        logger.warning(
            "Comment rate limit hit (ip): user_id=%s ip=%s slug=%s",
            ctx.user["id"], ctx.client_ip, slug,
        )
        return plain_response("Too many comments from this address. Please slow down.", 429)

    content_text = ctx.form.get("content", "").strip()
    max_len = config.get("max_length", 1000)
    if not content_text:
        return plain_response("Content cannot be empty", 400)
    if len(content_text) > max_len:
        return plain_response("Content too long", 400)

    # 解析回复目标
    reply_to = None
    reply_to_str = ctx.form.get("reply_to", "")
    if reply_to_str:
        reply_to = _safe_int(reply_to_str)
        if reply_to is None:
            return plain_response("Invalid reply target", 400)
        # 验证父评论存在且属于同一篇文章
        parent_comment = get_comment_by_id(reply_to)
        if not parent_comment or parent_comment["article_slug"] != slug:
            return plain_response("Invalid reply target", 400)

    try:
        create_comment(slug, ctx.user["id"], content_text, parent_id=reply_to)
    except Exception as e:
        logger.exception("Failed to create comment: %s", e)
        return plain_response("Internal Server Error", 500)

    return redirect_response(f"/article/{slug}#comments")


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
    if admin_only:
        if ctx.user["id"] != 1:
            return plain_response("Forbidden", 403), None
    elif ctx.user["id"] != 1 and ctx.user["id"] != comment["user_id"]:
        return plain_response("Forbidden", 403), None
    if not _form_csrf_ok(ctx):
        return plain_response("Invalid CSRF token", 400), None
    return None, comment


def comment_delete_post(ctx, slug: str, raw_id: str):
    """POST /article/{slug}/comment/delete/{id}（本人或管理员）"""
    error, comment = _comment_authorize(ctx, slug, raw_id)
    if error is not None:
        return error
    soft_delete_comment(comment["id"])
    return redirect_response(f"/article/{slug}#comments")


def comment_restore_post(ctx, slug: str, raw_id: str):
    """POST /article/{slug}/comment/restore/{id}（仅管理员 id=1）"""
    error, comment = _comment_authorize(ctx, slug, raw_id, admin_only=True)
    if error is not None:
        return error
    restore_comment(comment["id"])
    return redirect_response(f"/article/{slug}#comments")
