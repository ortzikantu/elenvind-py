"""auth 模块：登录 / 注册 / 登出。

公开入口：`routes.register(router)`。
密码、会话、CSRF、Cookie 全部来自 Core；本模块不含任何安全实现。
只依赖 Core。
"""
