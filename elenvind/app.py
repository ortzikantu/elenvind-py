"""Elenvind 应用装配点（Composition Root）。

这里是**唯一**允许同时认识 Core 与各业务模块的地方：

    elenvind/app.py      装配：Core App + 模块注册 + 启动钩子（本文件）
    elenvind/wsgi.py     生产入口：startup() + `application`（gunicorn 指向它）
    elenvind/core/       技术基座：配置 / HTTP / 路由 / 安全 / 模板 / 数据库
    elenvind/modules/    业务模块：blog / pages / auth / users / admin / seo / system
    elenvind/templates/  Jinja2 模板（Python 只准备数据，HTML 全在这里）
    elenvind/static/     静态资源（URL 镜像磁盘）

依赖方向（由 `tests/test_architecture.py` 静态守卫）：

    Application（本文件 / wsgi.py）
                ↓
        modules/（只依赖 Core，互不 import）
                ↓
            core/（不认识任何业务模块）

装配是**显式**的：注册顺序、错误页来源、模块间协作（例如 `/sitemap.xml` 的
文章条目）都在这里写清楚，不通过全局注册表或隐式约定。

注册顺序 = 优先级（保持与历史行为一致）：

1. 静态资源（`/<path:relative>`，fallback）——文件优先；
2. auth / users / admin（固定路径）；
3. blog（`/`、`/article/<slug>` 与评论写操作）；
4. seo（`/robots.txt`、`/sitemap.xml`）；
5. pages（`/theme` 与 `/<slug>` 兜底）——**最后**，否则兜底会遮蔽固定路径。
"""
from __future__ import annotations

from .core import assets
from .core.app import App
from .modules.admin import routes as admin_routes
from .modules.auth import routes as auth_routes
from .modules.blog import logic as blog_logic
from .modules.blog import routes as blog_routes
from .modules.pages import logic as pages_logic
from .modules.pages import routes as pages_routes
from .modules.seo import routes as seo_routes
from .modules.system import routes as system_routes
from .modules.users import routes as users_routes

#: 启动钩子：数据库/模板就绪后按序执行（模块的内容缓存预热）。
#: 显式交给 `core.lifespan.startup(hooks)` —— Core 里没有"模块注册表"这类全局状态。
STARTUP_HOOKS = (
    ("Articles loaded", blog_logic.load_articles),
    ("Custom pages loaded", pages_logic.load_pages),
)


def create_app() -> App:
    """构造应用：把各业务模块的路由与错误页装到 Core 的 App 上。"""
    #: 错误页由 system 模块提供（Core 只在渲染失败时回落纯文本）
    app = App(not_found=system_routes.not_found,
              forbidden=system_routes.forbidden,
              server_error=system_routes.server_error)

    router = app.router
    assets.register(router)
    auth_routes.register(router)
    users_routes.register(router)
    admin_routes.register(router)
    blog_routes.register(router, render_not_found=system_routes.not_found)
    #: 模块之间不互相 import：seo 需要的文章条目由这里显式注入
    seo_routes.register(router, articles=blog_logic.sitemap_articles)
    pages_routes.register(router, render_not_found=system_routes.not_found)
    return app


app = create_app()

__all__ = ["app", "create_app", "STARTUP_HOOKS"]
