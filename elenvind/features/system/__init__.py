"""System Feature 包：错误页（404 / 403 / 500）与对运维可见的状态。

对外只有 `routes`：`not_found` / `forbidden` / `server_error` 三个渲染函数，
由 `app.py` 在装配时交给 Core（`App(not_found=..., forbidden=..., server_error=...)`）。

这些页面**必须在没有请求上下文、甚至数据库不可用时也能渲染** ——
它们正是用来呈现代码出错时的状态的，因此不依赖任何业务数据。
"""
