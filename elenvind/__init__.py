"""Elenvind —— 一个安全默认的小型个人网站框架。

定位（不要把它当通用 Web Framework）：
- Python 标准库 + WSGI（Gunicorn 跑）+ Jinja2 + Markdown；
  不引入 ORM / DI / Event Bus / Plugin。
- 同步应用：没有 asyncio、没有异步数据库驱动、没有后台 writer 进程。
- SQLite 是唯一数据存储与不变量来源（WAL；写事务统一走 `db.write_tx()`）。
- SSR + Zero-JS。

分层与依赖方向（唯一允许的方向，由 `tests/test_architecture.py` 守卫）：

    elenvind/app.py, elenvind/wsgi.py      Application / Composition Root
                ↓                          （唯一允许同时认识 Core 与模块的地方）
        elenvind/core/                     运行时基座：配置 / HTTP / 路由 / 安全 / 会话 /
                ↓                          模板 / Markdown / 内容格式 / 日志
        elenvind/db/                       持久化基座（唯一 SQLite 边界）：连接 / 锁 /
                ↓                          写事务 write_tx() / schema 迁移 / 各表读写
        elenvind/modules/                  业务模块：blog / pages / auth / users /
                                            admin / seo / system

    标准库 + Gunicorn（WSGI）+ Jinja2 + Markdown

铁律：
- Core 不认识 db、也不认识业务模块；db 不认识 core/modules；
  模块可依赖 core 与 db，但**模块之间互不 import**
  （需要协作时由装配层显式注入，见 `elenvind/app.py`）。
- 业务模块负责业务，Core 负责 Web 安全与基础设施。
  模块不得自行实现 session、csrf、cookie、密码、安全响应头、请求限制。
"""

__all__ = ["__version__"]

from .core.version import get_version

__version__ = get_version()
