"""Blog 模块的 HTTP 路由（只做业务，安全全部交给 Core）。

路由声明即安全：
    @router.route("/article/<slug>", methods=["GET"])
    @router.route("/article/<slug>/comment", methods=["POST"], auth="required")
"""
from __future__ import annotations

import logging

from ...core.auth import require_owner
from ...core.config import config
from ...core.context import current_lang
from ...core.db_comment import get_comment_by_id
from ...core.http import Forbidden, NotFound, html, redirect
from ...core.i18n import t
from ...core.security import is_admin
from ...core.session import current_user
from ...core.templating import render_template
from . import logic

logger = logging.getLogger(__name__)


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register(router, *, render_not_found):
    """把一个 Blog 路由集合装到给定 router 上。

    只收 `render_not_found`（文章/页面不存在时渲染带布局的 404）。
    不收 `render_forbidden`：评论等写操作的 403 由 Core 的
    `router.dispatch(forbidden=...)` 统一处理。
    """

    @router.route("/", methods=["GET"])
    def home(request):
        page = _safe_int(request.arg("page"), 1)
        context = logic.article_list_context(page)
        return html(render_template("home.html", context))

    @router.route("/article/<slug>", methods=["GET"])
    def article(request, slug):
        context = logic.article_context(slug)
        if context is None:
            return render_not_found(request)
        context.update(_comment_section(request, slug))
        return html(render_template("article.html", context))

    @router.route("/article/<slug>/comment", methods=["POST"], auth="required")
    def create_comment(request, slug):
        if logic.get_article_by_slug(slug) is None:
            raise NotFound("Article not found")
        user = current_user(request)
        reply_to = None
        raw_reply = request.form.get("reply_to", "")
        if raw_reply:
            reply_to = _safe_int(raw_reply)
            if reply_to is None:
                return _article_error(request, slug, "Invalid reply target")
            parent = get_comment_by_id(reply_to)
            # 这里只做"能给出更友好提示"的预检 + 需要父行内容的判断。
            # 权威判定在 core/db_comment_rate.try_post_comment 的事务内
            # （存在性 + 同文章 + 深度上限），即使有人绕过表单直接 POST 也拦得住。
            if not parent or parent["article_slug"] != slug:
                return _article_error(request, slug, "Invalid reply target")
            # 刻意**不**校验 parent["is_deleted"]：删除是软删除、可恢复，
            # 已删除评论仍可被回复（对访客打码展示，关系链保持完整）。
            #
            # 深度上限也刻意只在这里"顺手一提"：预检在事务外，属于 TOCTOU
            # （并发时两个请求可能都看到"还没到顶"）。真正的判定在事务内，
            # 这里提前拦只是为了少走一趟事务、并给出同样的文案。
            if logic.comment_depth(parent) + 1 > logic._max_depth():
                return _article_error(request, slug, "Reply depth limit reached")

        outcome, message = logic.post_comment(slug, user["id"], request.client_ip,
                                              request.form.get("content", ""), reply_to)
        if outcome != "ok":
            # 日志措辞保持稳定（运维手册与排障习惯依赖它）
            if outcome == "rate_user":
                logger.warning("Comment rate limit hit (user): user_id=%s ip=%s slug=%s",
                               user["id"], request.client_ip, slug)
                return _article_error(request, slug, message, status=429)
            if outcome == "rate_ip":
                logger.warning("Comment rate limit hit (ip): user_id=%s ip=%s slug=%s",
                               user["id"], request.client_ip, slug)
                return _article_error(request, slug, message, status=429)
            if outcome == "too_many":
                logger.warning("Comment count limit hit: slug=%s", slug)
                return _article_error(request, slug, message, status=429)
            if outcome == "bad_parent":
                # 走到这里说明事务内的权威校验拒绝了它（存在性/跨文章）
                logger.warning("Comment rejected (bad parent): user_id=%s ip=%s "
                               "slug=%s parent_id=%s",
                               user["id"], request.client_ip, slug, reply_to)
                return _article_error(request, slug, message, status=400)
            if outcome == "too_deep":
                # 事务内的深度判定（预检在事务外，这里才是权威）
                logger.warning("Comment rejected (too deep): user_id=%s ip=%s "
                               "slug=%s parent_id=%s",
                               user["id"], request.client_ip, slug, reply_to)
                return _article_error(request, slug, message, status=400)
            return _article_error(request, slug, message, status=400)
        logger.info("Comment created: slug=%s user_id=%s parent_id=%s ip=%s",
                    slug, user["id"], reply_to, request.client_ip)
        return redirect(f"/article/{slug}#comments")

    @router.route("/article/<slug>/comment/delete/<comment_id>", methods=["POST"],
                  auth="required")
    def delete_comment(request, slug, comment_id):
        comment = _authorized_comment(request, slug, comment_id, admin_only=False)
        if not comment["is_deleted"]:
            logic.remove_comment(comment["id"])
            # 审核动作留痕（软删除可恢复，因此这是审计记录而不是"删除公告"）
            logger.info("Comment soft-deleted: id=%s slug=%s by user_id=%s",
                        comment["id"], slug, request.user["id"])
        return redirect(f"/article/{slug}#comments")

    @router.route("/article/<slug>/comment/restore/<comment_id>", methods=["POST"],
                  auth="required")
    def restore_comment(request, slug, comment_id):
        comment = _authorized_comment(request, slug, comment_id, admin_only=True)
        if comment["is_deleted"]:
            logic.restore_comment_by_id(comment["id"])
            logger.info("Comment restored: id=%s slug=%s by admin user_id=%s",
                        comment["id"], slug, request.user["id"])
        return redirect(f"/article/{slug}#comments")

    return router


def _comment_section(request, slug):
    """评论区上下文（登录用户才需要 CSRF 表单）。"""
    user = current_user(request)
    max_length = logic.default_max_length()
    rows, total, max_len = logic.build_comment_rows(slug, user, max_length=max_length)
    reply_to = _safe_int(request.arg("reply_to"))
    reply_to_message = ""
    if reply_to and user is not None:
        parent = get_comment_by_id(reply_to)
        if parent and parent["article_slug"] == slug:
            deleted = config.get("deleted_user_nickname", "Journeyed On")
            label = (f"{deleted} (#{parent['user_id']})" if parent["user_deleted"]
                     else f"{parent['nickname'] or 'Unknown'} (#{parent['user_id']})")
            reply_to_message = t(current_lang(), "comment_replying", name=label)
    return {
        "comment_rows": rows,
        "comment_total": total,
        "max_comment_length": max_len,
        "reply_to_value": str(reply_to or ""),
        "reply_to_message": reply_to_message,
    }


def _article_error(request, slug, message, *, status=400):
    """评论被拒：渲染文章页 + 明确提示（而不是裸文本）。"""
    context = logic.article_context(slug) or {}
    context.update(_comment_section(request, slug))
    context["message"] = message
    context["message_kind"] = "error"
    return html(render_template("article.html", context), status=status)


def _authorized_comment(request, slug, raw_id, *, admin_only):
    """删除/恢复前的归属校验（业务权限；认证已由 Core 完成）。"""
    comment_id = _safe_int(raw_id)
    if comment_id is None:
        raise Forbidden("Invalid comment")
    comment = get_comment_by_id(comment_id)
    if not comment:
        raise NotFound("Comment not found")
    if comment["article_slug"] != slug:
        raise Forbidden("Invalid comment")
    user = current_user(request)
    if is_admin(user):
        return comment
    if admin_only or not require_owner(user, comment):
        raise Forbidden("Forbidden")
    return comment
