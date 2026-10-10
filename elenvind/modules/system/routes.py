"""Error pages：404 / 403 / 500 的**带布局**渲染。

Core 在无法渲染页面时会回落到纯文本；一旦有请求上下文与模板，
就交给这里渲染完整页面（保持站点观感一致）。
"""
from __future__ import annotations

import logging

from ...core.context import current_lang
from ...core.http import html
from ...core.i18n import t
from ...core.templating import render_template

logger = logging.getLogger(__name__)


def not_found(request):
    """404：优先渲染带布局的页面。"""
    return html(render_template("errors/404.html", {}), status=404)


def bad_request(request):
    """400：表单令牌失效 / 请求体不合法时的**友好页**。

    UX 背景：CSRF 令牌会过期（页面开了很久、浏览器清过 Cookie、手改过表单），
    此时只回一行 `Invalid CSRF token` 会让用户完全不知道怎么办。
    这里给出一致的页面与"返回上一页重新提交"的指引；
    **安全语义不变**：依旧 400、不执行任何状态变更、不泄漏任何上下文。
    """
    return html(render_template("errors/400.html", {}), status=400)


def forbidden(request):
    """403：未登录/权限不足访问受保护页面时使用（文案走 i18n）。"""
    lang = current_lang()
    return html(render_template("errors/error.html", {
        "error_title": t(lang, "page403_title"),
        "error_message": t(lang, "page403_desc"),
    }), status=403)


def server_error(request=None, message=None):
    """500：渲染错误页；渲染本身失败时回落到纯文本。

    `message` 只用于代码内的固定文案覆盖（默认走 i18n）。**不要**把异常对象或
    异常字符串传进来 —— 那是 Core 与模块之间的契约（错误页不含任何请求细节）。
    """
    lang = current_lang()
    try:
        return html(render_template("errors/error.html", {
            "error_title": t(lang, "page500_title"),
            "error_message": message or t(lang, "page500_desc"),
        }), status=500)
    except Exception:      # 错误页渲染失败不能再抛
        from ...core.http import text
        return text("Internal Server Error", status=500)
