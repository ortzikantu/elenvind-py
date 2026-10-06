"""admin 模块：管理员页面。

公开入口：`routes.register(router)`。权限由路由声明（`permission="admin"`）
交给 Core 的调度器执行，本模块不实现认证。
只依赖 Core。
"""
