"""system 模块：错误页（404 / 403 / 500）与对运维可见的状态。

公开入口是 `routes` 里的三个渲染函数：`not_found` / `forbidden` / `server_error`，
由装配层（`elenvind/app.py`）交给 Core
（`App(not_found=..., forbidden=..., server_error=...)`）。

这些页面**必须在没有请求上下文、甚至数据库不可用时也能渲染** ——
它们正是用来呈现代码出错时的状态的，因此不依赖任何业务数据。
因为它们是错误页提供者而不是路由集合，所以没有 `register()`（不强行统一形状）。
只依赖 Core。
"""
