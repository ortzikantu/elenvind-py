"""Pages Feature 路由：/theme 与自定义页面兜底。

`/theme` 是 GET 请求写"偏好"Cookie（不是状态变更）：只写 theme、只跳站内路径，
因此不需要 CSRF；但它确实会写 Cookie，所以这里显式说明其边界。
"""
from __future__ import annotations

from ...core.http import html, redirect
from ...core.security import THEME_COOKIE, THEME_MAX_AGE
from ...core.templating import render_template
from . import logic


def register(router, *, render_not_found):
    @router.route("/theme", methods=["GET"])
    def theme(request):
        """切换主题偏好并跳回站内页面（防开放重定向）。"""
        mode = request.arg("mode", "")
        target = request.arg("next", "/") or "/"
        # 只接受站内绝对路径：不是"/"开头、或以"//"开头（协议相对 URL）都回首页
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        target = target.replace("\r", "").replace("\n", "")
        response = redirect(target)
        if mode in ("light", "dark"):
            # Cookie 名与有效期都取自 Core 常量，Feature 不自造
            response.set_cookie(THEME_COOKIE, mode, max_age=THEME_MAX_AGE)
        return response

    @router.route("/<slug>", methods=["GET"], fallback=True)
    def custom_page(request, slug):
        body = logic.get_page(slug)
        if body is None:
            return render_not_found(request)
        return html(render_template("page.html", {
            "page": {"slug": slug, "body": body},
            "page_title": slug,
        }))

    return router
