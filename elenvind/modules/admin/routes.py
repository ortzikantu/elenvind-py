"""Admin 模块：管理员专属页面。

**本模块不实现任何认证逻辑**——它只声明 `permission="admin"`，
由 Core 的调度器在执行前判定，普通用户自动 403。
"""
from __future__ import annotations

from ...core.http import html
from ...core.templating import render_template


def register(router):
    @router.route("/admin", methods=["GET"], auth="required", permission="admin")
    def admin_home(request):
        from ...core.config import config
        from ...core.db_user import get_user_number

        return html(render_template("admin/index.html", {
            "admin": {
                "user_id": request.user["id"],
                "user_count": get_user_number(),
                "site_url": str(config.get("site_url", "")),
                "registration_enabled": bool(config.get("registration_enabled", True)),
                "max_comment_depth": config.get("max_comment_depth", 32),
                "max_comments_per_article": config.get("max_comments_per_article", 1000),
            },
        }))

    return router
