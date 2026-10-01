"""Elenvind —— 一个安全默认的小型个人网站框架。

定位（不要把它当通用 Web Framework）：
- Python 标准库 + Uvicorn（ASGI）+ Jinja2 + Markdown；不引入 ORM / DI / Event Bus / Plugin。
- SQLite 是唯一数据存储与不变量来源。
- SSR + Zero-JS。

分层（唯一允许的依赖方向）：

    features/          业务：blog / pages / users / auth / admin
        ↓ 只使用
    core/              Web 安全与基础设施：request / response / routing /
                       session / csrf / auth / authorization / cookie /
                       security headers / templating / markdown / db
        ↓
    标准库 + Uvicorn + Jinja2 + Markdown

铁律：Feature 负责业务，Core 负责 Web 安全。
Feature 不得自行实现 session、csrf、cookie、密码、安全响应头、请求限制。
"""

__all__ = ["__version__"]

from .core.version import get_version

__version__ = get_version()
