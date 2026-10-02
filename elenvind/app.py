"""Elenvind 应用装配点。

这里是"框架 + Feature"唯一汇合的地方：

    elenvind/app.py      装配：Core App + Feature 注册 + 启动钩子
    elenvind/core/       Web 安全与基础设施（不需要认识任何 Feature）
    elenvind/features/   业务（只使用 Core）
    elenvind/templates/  Jinja2 模板

ASGI 入口：`elenvind.app.app`（run.py / uvicorn 都指向它）。
"""
from __future__ import annotations

from .core.app import App
from .features import registry


def create_app() -> App:
    """构造应用：注册 Feature 路由、错误处理与启动钩子。"""
    handlers = registry.error_handlers()
    app = App(not_found=handlers["not_found"], forbidden=handlers["forbidden"],
              server_error=handlers["server_error"])
    registry.install(app.router)
    registry.register_startup_hooks()
    return app


app = create_app()

__all__ = ["app", "create_app"]
