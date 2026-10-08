"""Elenvind Core：稳定的技术基座。

**职责**（只放应用运行所必需的通用机制）：

| 领域 | 模块 |
|---|---|
| 配置与运行环境 | `config`、`version`、`console` |
| WSGI application / 请求边界 | `app`（`App.__call__`）、`http`（Request/Response/安全头） |
| 路由与静态资源 | `routing`、`assets` |
| 会话 / Cookie / CSRF / 认证授权 | `session`、`security`、`csrf`、`auth` |
| 模板与内容格式 | `templating`、`content`、`markdown`、`i18n`、`context` |
| 启动与关闭 | `lifespan`、`logging_config` |
| 通用辅助 | `utils` |

**边界**（由 `tests/test_architecture.py` 与 `tests/test_core_contract.py` 守卫）：

- Core **不认识 db，也不认识业务模块**：不 import `elenvind.db.*` / `elenvind.modules.*`，也不 import 装配层；
  数据库相关的一切在 `elenvind/db/`，core 需要的值（会话 store、用户总数、
  数据库启动步骤）由装配层注入；
- Core 不持有"模块注册表"这类全局可变状态：启动钩子由装配层通过
  `lifespan.startup(hooks)` 显式传入；
- 具体业务规则（文章索引、注册流程、评论呈现…）属于 `modules/`；
  持久化属于 `elenvind/db/`（唯一 SQLite 边界）。

业务模块只从本包取能力；安全机制（session / csrf / cookie / 密码 /
安全响应头 / 请求限制）在这里且只在这里实现一次。
"""
