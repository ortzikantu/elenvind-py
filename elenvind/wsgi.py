"""WSGI 生产入口 —— Gunicorn 指向的就是这里：

    gunicorn --workers 2 --bind 127.0.0.1:6789 elenvind.wsgi:application

导入顺序（**必须**先初始化再暴露 callable）：

    startup(STARTUP_HOOKS)   配置 / 日志 / i18n / 模板 / 数据库初始化与迁移 / 缓存预热
    application              WSGI callable（`elenvind.core.app.App` 实例）
    atexit shutdown()        进程退出前 flush + 关闭日志 handler

启动钩子来自装配层（`elenvind/app.py`）：Core 只提供 `startup(hooks)` 机制，
不知道有哪些业务模块。

为什么把 `startup()` 放在导入时：WSGI 只有"导入模块 + 调用 callable"两个
时机，没有独立的启动/关闭协议，服务器只会导入并调用这个 callable。
未开 `--preload` 时 gunicorn 在**每个 worker** 里各导入一次，因此 startup()
会被每个 worker 执行一次 —— 这是安全的：

- `startup()` 的每一步都是幂等的（重读配置、重装日志、`CREATE TABLE IF NOT
  EXISTS`、`user_version` 迁移、缓存预热）；
- 启动即失败（配置非法、模板语法错误、迁移半成品）会抛异常 => 该 worker
  导入失败 => gunicorn 拒绝启动，不会留下"看起来在跑但每个请求都出错"的进程。

想只初始化一次可以开 `--preload`（master 导入，worker 继承），见部署文档。
"""
from __future__ import annotations

import atexit

from .app import STARTUP_HOOKS, app
from .core.lifespan import shutdown, startup

startup(STARTUP_HOOKS)
atexit.register(shutdown)

#: WSGI callable（PEP 3333）：`gunicorn elenvind.wsgi:application`
application = app

__all__ = ["application", "app"]
