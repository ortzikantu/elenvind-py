# 架构总览

> **适用读者**：要改这个代码库的人（新模块、新能力、review、接手维护）。
> **一句话**：Gunicorn / WSGI → Composition Root →（Core / db / Modules）→ SQLite，
> 依赖方向单向；SQLite 只出现在 `elenvind/db/`，所有业务写入经过 `write_tx()`。

---

## 1. 设计原则（先读这 6 条，后面都是它们的推论）

1. **KISS**：能用一个函数解决的，不引入类；能显式传参的，不引入注册表。
2. **标准库优先**：运行期依赖只有 Gunicorn / Jinja2 / Markdown / MarkupSafe。
3. **同步**：业务代码是普通 `def`。没有 asyncio，没有异步驱动，没有后台 writer。
4. **WSGI**：生产是 Gunicorn sync worker；不恢复 ASGI/Uvicorn。
5. **安全能力在 Core 实现一次**：session / CSRF / Cookie / 密码 / 安全头 / 请求限制
   由 Core 统一施加，业务模块不重复实现、也不能绕过（有静态守卫）。
6. **数据完整性靠协议而不是靠约定**：所有写事务走同一个入口，跨进程互斥由内核
   `flock` 提供。

**明确不做**（改动时不要"顺手"引入）：ORM、DI 容器、Service Locator、Event Bus、
Repository / Unit of Work、应用层队列 / writer 进程 / 重试框架、插件系统、
把 Core 再切成更多架构层。

---

## 2. 分层与依赖方向

```
                  Gunicorn（sync worker）× N
                              │  WSGI: environ, start_response
                              ▼
    Application / Composition Root
      elenvind/wsgi.py        生产入口：startup(STARTUP_HOOKS, prepare_database) → application
      elenvind/app.py         装配：数据库路径与启动、路由注册顺序、错误页、
                              会话 store 注入、模块间注入、启动钩子
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
    core/                 db/                  modules/
    运行时基座            持久化基座            业务功能
    （不认识 db、         （唯一 SQLite 边界；   （可依赖 core 与 db；
      不认识 modules）      不认识 core/modules）   模块之间零 import）
        │                     │                     │
        └─────────────────────┴─────────────────────┘
                              ▼
                         SQLite（WAL）
                              ▲
              读：db.connect()   写：db.write_tx()（唯一入口）
```

```
elenvind/
  app.py       组合入口
  wsgi.py      生产入口
  core/        运行时基座：config · http · routing · security · session · csrf ·
               auth · templating · markdown · content · context · i18n · lifespan ·
               logging_config · console · utils · assets · version
  db/          持久化基座：connection · transaction · migration · user · session ·
               comment · comment_rate · auth · maintenance
  modules/     业务模块：blog · pages · auth · users · admin · seo · system
  templates/   Jinja2 模板
  static/      静态资源
```

### 依赖规则（全部由测试守卫）

| 规则 | 守卫 |
|---|---|
| **Core ↛ db**、Core ↛ Modules、Core ↛ Application | `test_core_only_depends_on_itself` |
| **db ↛ core**、db ↛ Modules、db ↛ Application（db 只依赖标准库） | `test_db_is_independent` |
| 只有 `db/` 可以 `import sqlite3` | `test_only_db_imports_sqlite3` |
| 只有 `db/` 可以执行 SQL / 提交或回滚事务 | `test_sql_and_transactions_stay_inside_db` |
| `BEGIN IMMEDIATE` 只出现在 `db/transaction.py` | `test_begin_immediate_only_in_the_transaction_module` |
| db 不处理 HTTP / Cookie / 模板 | `test_db_does_not_handle_http_or_templates` |
| Modules ↛ Application | `test_modules_do_not_import_the_composition_root` |
| Module A ↛ Module B | `test_modules_do_not_import_each_other`（零例外，无允许清单） |
| 模块级 import 图无环；Modules 层（含延迟 import）无环 | `ImportCycleTests` |
| Core 既有的"延迟 import 环"不增加 | `test_core_lazy_import_cycles_do_not_grow`（棘轮） |
| Modules 不执行 SQL、不开连接、不重造安全能力 | `tests/test_core_contract.py` |
| 所有写 SQL 都在 `write_tx()`/事务连接内 | `tests/test_core_contract.py` |
| 每个模块的公开入口存在且可调用 | `test_module_public_entrypoints_are_callable` |

**为什么"模块之间零 import"**：模块互相 import 会立刻产生隐式依赖网与循环依赖
风险，测试也再难单独 import 一个模块。需要协作时，把数据源作为参数从装配层注入。

---

## 3. 组合入口（Composition Root）：`elenvind/app.py`

唯一同时 `import core` 与 `import modules` 的地方。它显式做三件事：

```python
STARTUP_HOOKS = (                       # 启动钩子由装配层提供，Core 只提供机制
    ("Articles loaded", blog_logic.load_articles),
    ("Custom pages loaded", pages_logic.load_pages),
)

def create_app() -> App:
    app = App(not_found=system_routes.not_found,          # 错误页来自 system 模块
              forbidden=system_routes.forbidden,
              server_error=system_routes.server_error)
    router = app.router
    assets.register(router)                               # ① 顺序即优先级
    auth_routes.register(router)
    users_routes.register(router)
    admin_routes.register(router)
    blog_routes.register(router, render_not_found=system_routes.not_found)
    seo_routes.register(router, articles=blog_logic.sitemap_articles)   # ② 跨模块注入
    pages_routes.register(router, render_not_found=system_routes.not_found)
    return app
```

要点：

- **① 注册顺序 = 优先级**：静态资源与固定路径必须在 `pages` 的 `/<slug>` 兜底之前，
  否则兜底会遮蔽 `/login`、`/article/...`。顺序在 `app.py` 里一眼可见，不需要读调度器。
- **② 跨模块协作靠注入**：`/sitemap.xml` 需要文章清单，但 seo 不 import blog —— 装配层
  把 `blog.logic.sitemap_articles` 传进去。协作者永远不互相认识。
- **③ 启动钩子显式传入**：`core.lifespan.startup(hooks)` 接一个序列，Core 里没有
  "模块注册表"这类全局可变状态。

---

## 4. 一个请求的完整路径

```
environ, start_response
  │
  ├─ App.__call__（core/app.py，同步函数）
  │    ├─ Request.from_wsgi()        解析 + 请求边界校验（Content-Length / 类型 / 体积上限）
  │    ├─ bind_request()             请求级上下文（模板全局取当前请求）
  │    ├─ load_user()                会话 Cookie → 用户行（滑动过期在这里刷新）
  │    ├─ Router.dispatch()          路由匹配 + 三道闸门：
  │    │     ① CSRF（非安全方法，失败 400）
  │    │     ② 认证（匿名 GET → 302 /login?next=…；匿名写 → 403）
  │    │     ③ 权限（permission="admin" 等，档位未知一律拒绝）
  │    ├─ send_response()            安全响应头 + 待下发 Cookie + status/headers/body
  │    └─ 访问日志一行（无论成功、404、500 都记）
  │
  └─ 异常 → 500：优先渲染模块的错误页；渲染失败回落纯文本；堆栈只进日志
```

模块只在 `Router.dispatch()` 里被调用；它拿到 `request`、返回 `Response`（或抛
`HttpError` 家族），不需要知道 CSRF、Cookie、安全头、请求限制的存在。

---

## 5. 模块清单

| 模块 | 职责 | 公开入口 |
|---|---|---|
| `blog` | 文章索引 / 正文渲染 / 文章页 + 评论（发表、回复、软删除、恢复） | `logic`、`routes.register(router, render_not_found=…)`、`logic.sitemap_articles()` |
| `pages` | 自定义页面（`custom_pages/*.md`）+ 主题偏好入口 | `logic`、`routes.register(router, render_not_found=…)` |
| `auth` | 登录 / 注册 / 登出 | `routes.register(router)` |
| `users` | 个人中心（资料 / 改密 / 删号） | `routes.register(router)` |
| `admin` | 管理员页面（权限由路由声明交给 Core） | `routes.register(router)` |
| `seo` | `/robots.txt`、`/sitemap.xml`（数据由装配层注入） | `routes.register(router, articles=…)` |
| `system` | 404 / 403 / 500 错误页 | `routes.not_found / forbidden / server_error` |

形状**不强行统一**：`system` 是错误页提供者而不是路由集合，所以它没有 `register()`；
`seo` 没有 `logic` 模块，因为它不持有状态。加模块时在 `tests/test_architecture.py`
的 `MODULE_ENTRYPOINTS` 里登记一行，守卫会检查它真实存在。写法见
[development/modules.md](development/modules.md)。

---

## 6. Core 的职责与公开面

| 领域 | 模块 |
|---|---|
| 配置与运行环境 | `config`、`version`、`console` |
| WSGI application / 请求边界 | `app`（`App.__call__`）、`http`（`Request` / `Response` / 安全头出口） |
| 路由与静态资源 | `routing`、`assets` |
| 会话 / Cookie / CSRF / 认证授权 | `session`、`security`、`csrf`、`auth` |
| 模板与内容格式 | `templating`、`content`、`markdown`、`i18n`、`context` |
| 启动关闭 / 日志 / 工具 | `lifespan`、`logging_config`、`utils` |

**数据库相关的一切都在 `elenvind/db/`**（一级包，见 §7）：`connection`（路径/锁文件/只读
连接）、`transaction`（`write_tx()` 唯一写入口）、`migration`（schema 与 `user_version`
迁移）、`user`、`session`、`comment`、`comment_rate`、`auth`（登录/注册流水）、
`maintenance`（机会式清理）。

### 为什么有独立的 `db/` 层（而不是把 sqlite 代码留在 core）

`core/` 是"项目专属运行时基座"，`db/` 是"持久化基座"。分开的三个理由：

1. **唯一 SQLite 边界**：`import sqlite3`、SQL 执行、`BEGIN`/`COMMIT`、schema 迁移
   全部收在 `db/`，守卫测试保证 core 与 modules 里不会出现这些（越界即变红）；
2. **core 不再随持久化代码膨胀**：core 只保留 HTTP/安全/模板/配置这类运行时机制；
3. **依赖方向干净**：`core ↛ db`。core 需要数据库信息时由装配层**注入**：
   会话持久化注入 `store=db`（`core.session.load_user(request, store=…)`）、
   用户总数注入 provider（`core.context.set_user_count_provider`）、
   数据库启动注入 `prepare_database`（`core.lifespan.startup(..., prepare_database=)`）。

判定标准（"什么该进 Core / db / modules"）：

| 情况 | 放哪 |
|---|---|
| 与业务无关的运行时机制（HTTP、路由、安全、模板、配置、会话语义） | **core** |
| 任何 SQLite 相关实现（连接、锁、事务、schema、迁移、各表读写） | **db** |
| 多个模块共用的**格式/协议原语**（例：`core.content` 的 TOML 文档头 + slug 校验） | **core** |
| 只服务某一个业务的具体规则（文章索引、评论树、登录流程、sitemap 组装） | **modules** |
| 只是"看起来通用"但只有一处用 | **留在用它的模块**，不要提前抽象 |

---

## 7. 数据写入路径（C0 概览）

```
module（业务） ──► db 的领域 API（db.user/db.comment/…）──► write_tx()
                                          ├─ flock(LOCK_EX) on <db>.write.lock
                                          ├─ 打开连接（PRAGMA foreign_keys / busy_timeout）
                                          ├─ BEGIN IMMEDIATE
                                          ├─ yield conn（业务 SQL 全在锁内、同一事务内）
                                          ├─ COMMIT / ROLLBACK
                                          ├─ 关闭连接
                                          └─ 释放 flock（无论异常/键盘中断/被 SIGKILL）
```

- **读**：`with db.connect() as conn:`（只读，不参与 flock）。
- **写**：`with db.write_tx() as conn:`（唯一合法入口；模块不得自己开连接或 `BEGIN`）。
- **迁移/建表**：同样在 `write_tx()` 内，多 worker 并发启动由 flock 串行化。

契约细节、锁文件规则、"保证什么/不保证什么"、可观测性与迁移机制见
[development/database.md](development/database.md)。

---

## 8. 关键不变量（invariants）

改代码时若破坏其中任何一条，都会有测试变红或部署语义变化：

1. `elenvind/wsgi.py` 暴露 `application`，且是**同步** WSGI callable。
2. Core 不认识 db、不认识业务模块；db 不认识 core/modules；模块之间零 import。
3. 每个模块可独立 `import`（无隐含导入顺序）。
4. 业务写入只能通过 `db.write_tx()`；`db.connect()` 只用于读；SQL 只出现在 `db/`。
5. 锁文件由数据库路径派生、永不删除、多 worker 得到同一路径、不同库得到不同路径。
6. 安全能力（CSRF / Cookie / 密码 / 安全头 / 请求限制 / 会话）只在 Core 实现一次。
7. 安全响应头对**每个**响应生效，包括 404 / 403 / 500。
8. 配置在启动时校验一次：非法配置拒绝启动，而不是跑到某个页面才报错。
9. 内容缓存（文章 / 页面）按文件状态失效，改文件即生效，无需重启。
