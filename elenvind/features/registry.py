"""Feature 注册表：Core 不需要认识任何 Feature，由这里做唯一的装配。

新增一个 Feature 的步骤（见 docs/development/features.md）：
1. 在 `elenvind/features/<name>/` 实现业务（logic / templates）；
2. 提供 `register(router, ...)` 把路由声明装上去；
3. 在本文件 `install()` 里加一行；
4. 如果有内容缓存，用 `core.lifespan.register_startup_hook()` 注册预热。

本模块是**唯一**知道"有哪些 Feature"的地方——依赖方向保持 Feature -> Core。
"""
from __future__ import annotations

from .admin import routes as admin_routes
from .auth import routes as auth_routes
from .blog import logic as blog_logic
from .blog import routes as blog_routes
from .pages import logic as pages_logic
from .pages import routes as pages_routes
from .seo import routes as seo_routes
from .system import routes as system_routes
from .users import routes as users_routes


def install(router) -> None:
    """把所有 Feature 的路由装到 router 上。

    顺序说明：固定路径优先，自定义页面兜底 `/<slug>` 放最后。
    实际上兜底标记了 `fallback=True`，不会被其它路由的方法语义影响，
    这里的顺序只是让路由表读起来更直观。
    """
    from ..core import assets

    not_found = system_routes.not_found

    assets.register(router)              # 静态资源（挂在站点根）
    auth_routes.register(router)
    users_routes.register(router)
    admin_routes.register(router)
    blog_routes.register(router, render_not_found=not_found)
    seo_routes.register(router)
    pages_routes.register(router, render_not_found=not_found)   # 兜底：放最后


def register_startup_hooks() -> None:
    """内容缓存预热：由 lifespan 在数据库就绪后调用。"""
    from ..core.lifespan import register_startup_hook

    register_startup_hook("Articles loaded", blog_logic.load_articles)
    register_startup_hook("Custom pages loaded", pages_logic.load_pages)


def error_handlers():
    """给 App 用的错误处理函数（带布局的 404/403/500）。"""
    return {
        "not_found": system_routes.not_found,
        "forbidden": system_routes.forbidden,
        "server_error": system_routes.server_error,
    }
