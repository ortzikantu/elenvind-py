"""Elenvind 业务模块包（modules/）。

每个子包 = 一个**业务职责**，只依赖 `elenvind.core`：

| 模块 | 职责 |
|---|---|
| `blog` | 文章索引 / 正文渲染 / 文章页 + 评论（发表、回复、软删除、恢复） |
| `pages` | 自定义页面（`custom_pages/*.md`）与主题偏好入口 |
| `auth` | 登录 / 注册 / 登出 |
| `users` | 个人中心（资料、改密、删号） |
| `admin` | 管理员页面 |
| `seo` | `/robots.txt`、`/sitemap.xml` |
| `system` | 404 / 403 / 500 错误页 |

**规则**（由 `tests/test_architecture.py` 守卫）：

- 模块之间**互不 import**：协作由装配层（`elenvind/app.py`）显式注入，
  例如 seo 的文章条目来自 `blog.sitemap_articles`；
- 模块不 import 装配层（`elenvind.app` / `elenvind.wsgi`）取全局对象；
- 模块不直接开数据库连接、不执行 SQL：走 Core 的 `db_*` 业务 API，
  写操作最终进入 `core.db_base.write_tx()`；
- 每个模块的公开入口是 `routes.register(router, ...)`（参数按需，不强制统一形状）；
- `__init__.py` 只写文档，不做副作用导入 —— 本包是纯命名空间。

本文件**不** import 任何子模块：导入包不应触发业务模块的副作用。
"""
