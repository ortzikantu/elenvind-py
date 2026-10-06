"""Error pages：404 / 403 / 500 的**带布局**渲染。

Core 在无法渲染页面时会回落到纯文本；一旦有请求上下文与模板，
就交给这里渲染完整页面（保持站点观感一致）。
"""
from __future__ import annotations

import logging

from ...core.http import html
from ...core.templating import render_template

logger = logging.getLogger(__name__)


def not_found(request):
    """404：优先渲染带布局的页面。"""
    return html(render_template("errors/404.html", {}), status=404)


def forbidden(request):
    """403：未登录访问受保护页面时使用。"""
    return html(render_template("errors/error.html", {
        "error_title": "Forbidden",
        "error_message": "You do not have permission to view this page.",
    }), status=403)


def server_error(request=None, message="Internal Server Error"):
    """500：渲染错误页；渲染本身失败时回落到纯文本。"""
    try:
        return html(render_template("errors/error.html", {
            "error_title": "500 Internal Server Error",
            "error_message": message,
        }), status=500)
    except Exception:      # noqa: BLE001 - 错误页渲染失败不能再抛
        from ...core.http import text
        return text("Internal Server Error", status=500)
