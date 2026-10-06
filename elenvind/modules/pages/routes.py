"""Pages 模块路由：/theme 与自定义页面兜底。

`/theme` 用 GET 写"站内偏好"Cookie。这是**刻意**的宽松设计，边界写在下面：

- 它写的是**这个浏览器自己的显示偏好**，不是服务端状态：不影响任何其他人、
  不改变任何数据、不产生任何可被第三方利用的后果；
- 危害模型是"CSRF 强制切换某人的配色"，而这个cookie本来就只能由该浏览器自己
  的偏好决定，被改一次用户再点一次就回来了 —— 因此不值得为它引入 CSRF 令牌
  （那会让 `<a href="/theme?mode=dark">` 这种最简单的链接失效，并需要
  在页面上放一个表单+令牌，代价远大于收益）；
- 尽管如此，它**确实**写了 Cookie，所以这里显式说明，而不是默默为之。

除偏好外，任何会改变状态的路径都必须由非安全方法 + CSRF 触发。
"""
from __future__ import annotations

from ...core.http import html, redirect, safe_next_path
from ...core.templating import render_template
from . import logic


def register(router, *, render_not_found):
    @router.route("/theme", methods=["GET"])
    def theme(request):
        """切换主题偏好并跳回站内页面（防开放重定向）。

        - `next` 是用户可控输入，必须过 Core 的 `safe_next_path()`。
          这里曾经自己写了一份更弱的校验（不拒绝反斜杠、只拒绝 CR/LF），
          而反斜杠与 TAB 都会被浏览器归一化/剥离成 `//host`（跨站跳转）。
        - Cookie 由 Core 下发：模块只说"把 theme 偏好设为 dark"，
          不知道 Cookie 名字、有效期与属性（见 `PREFERENCE_COOKIES`）。
        """
        target = safe_next_path(request.arg("next", "/"), default="/")
        request.set_preference("theme", request.arg("mode", ""))
        return redirect(target)

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
