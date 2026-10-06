"""Elenvind Core：稳定的技术基座。

**职责**（只放应用运行所必需的通用机制）：

| 领域 | 模块 |
|---|---|
| 配置与运行环境 | `config`、`version`、`console` |
| WSGI application / 请求边界 | `app`（`App.__call__`）、`http`（Request/Response/安全头） |
| 路由与静态资源 | `routing`、`assets` |
| 会话 / Cookie / CSRF / 认证授权 | `session`、`security`、`csrf`、`auth` |
| 模板与内容格式 | `templating`、`content`、`markdown`、`i18n`、`context` |
| 数据库（连接 / 写协调 / 迁移） | `db_base`（`connect()` / `write_tx()`）、`db_*` |
| 启动与关闭 | `lifespan`、`logging_config` |
| 通用辅助 | `utils` |

**边界**（由 `tests/test_architecture.py` 与 `tests/test_core_contract.py` 守卫）：

- Core **不认识任何业务模块**：不 import `elenvind.modules.*`，也不 import 装配层；
- Core 不持有"模块注册表"这类全局可变状态：启动钩子由装配层通过
  `lifespan.startup(hooks)` 显式传入；
- 具体业务规则（文章索引、注册流程、评论呈现…）属于 `modules/`；但
  **数据访问层**（`db_*.py`）作为 Core 的数据库 API 保留在这里 —— 它是 C0 写协调
  边界（`write_tx()`）的唯一出口，语义见 `db_base` 的模块说明。

业务模块只从本包取能力；安全机制（session / csrf / cookie / 密码 /
安全响应头 / 请求限制）在这里且只在这里实现一次。
"""
