# 模块开发指南

> **适用读者**：要新增/修改业务能力的人。
> **一句话契约：模块负责业务，Web Core 负责 Web 安全。**
> **相关文档**：[架构总览](../ARCHITECTURE.md) · [数据库与 C0](database.md) ·
> [测试体系](testing.md) · [安全模型](../SECURITY.md) · [文档索引](../README.md)

写业务代码时你不需要（也不允许）考虑 CSRF、Cookie 属性、安全响应头、
请求体上限、会话轮换、密码哈希。这些都已在 Core 里实现一次，
并由 `tests/test_core_contract.py` 静态 + 运行时双重守卫。

## 目录

1. [目录结构与依赖方向](#1-目录结构与依赖方向)
2. [五分钟写一个模块](#2-五分钟写一个模块)
3. [路由声明即安全](#3-路由声明即安全)（含未登录体验、状态变更、权限三档）
4. [可以用什么，不可以用什么](#4-可以用什么不可以用什么)（含 Cookie 唯一出口、日志怎么记）
5. [模板约定](#5-模板约定)
6. [内容格式](#6-内容格式文章与页面)（文章与页面、`@video` 指令）
7. [数据访问](#7-数据访问读-connect写-write_tx)（`connect()` / `write_tx()`、C0 保证什么、可观测性）
8. [国际化](#8-国际化)
9. [测试](#9-测试)
10. [常见错误与对应现象](#10-常见错误与对应现象)

---

## 1. 目录结构与依赖方向

```
elenvind/
  app.py                组合入口（Composition Root）：唯一同时认识 core 与 modules 的地方
                        装配顺序、错误页来源、模块间协作的注入点都在这里
  wsgi.py               生产入口：startup(STARTUP_HOOKS) + application（Gunicorn 指向它）
  core/                 技术基座：与业务无关的通用机制
    app.py              WSGI application 与请求流水线编排
    routing.py          @route 声明 + 调度器（CSRF / 认证 / 权限闸门）
    http.py             Request(from_wsgi) / Response / 安全头 / Cookie 下发
    security.py         密码哈希、Cookie 构造、CSP 与安全头定义
    session.py          服务端会话（轮换、两级过期、清理）
    auth.py             认证与授权（verify_credentials / check_permission）
    csrf.py             CSRF 单点实现 + 模板全局 csrf_input()
    templating.py       Jinja2 唯一入口 render_template()
    markdown.py         Markdown 唯一入口 render_markdown() + 白名单净化
    content.py          内容格式原语（TOML 文档头 + slug 校验），blog 与 pages 共用
    db_base.py          DB 层引导：connect() 只读 / write_tx() 唯一写入口 / schema 与迁移
    db_*.py             各表的数据访问（写操作统一走 write_tx()）
    config.py           配置加载与校验
    context.py          请求级上下文（模板全局取当前请求）
    lifespan.py         启动/关闭流程（启动钩子由装配层传入）
    i18n.py             文案表
  modules/              业务模块：只依赖 core，模块之间互不 import
    blog/               文章索引、正文渲染、文章页 + 评论
    pages/              自定义页面 + 主题偏好
    auth/               登录 / 注册 / 登出
    users/              个人中心
    admin/              管理员页面
    seo/                robots.txt / sitemap.xml（文章条目由装配层注入）
    system/             404 / 403 / 500 页面（供装配层交给 Core）
  templates/            Jinja2 模板（Python 只准备数据，HTML 全在这里）
  static/               静态资源（URL 镜像磁盘）
```

依赖方向**单向**，由 `tests/test_architecture.py` 静态守卫：

```
Application（elenvind/app.py、elenvind/wsgi.py）
      │  ← 装配：注册路由、注入模块间的数据源、提供错误页
      ▼
modules/  （只依赖 core；模块 A 不 import 模块 B）
      │
      ▼
core/     （不认识任何业务模块，也不 import 装配层）
      │
      ▼
标准库 + Gunicorn（WSGI）+ Jinja2 + Markdown
```

四条硬规则：

| 规则 | 含义 |
|---|---|
| Core 不 import 模块 | `core/**` 里不得出现 `elenvind.modules.*` / `..modules` |
| 模块不 import 装配层 | 模块不得 `import elenvind.app` / `elenvind.wsgi` 取全局对象 |
| **模块之间互不 import** | 需要协作时由装配层传参注入（例：`seo` 的文章条目） |
| 无循环 import | 模块级 import 图必须无环（Core 的既有延迟 import 有棘轮守卫） |

**同步优先**：模块的 handler 是普通 `def`；不要为了"看起来现代"引入
async/await —— 应用是同步 WSGI 应用，有静态守卫测试盯着。

---

## 2. 五分钟写一个模块

### 2.1 写逻辑（`modules/greeting/logic.py`）

```python
from ...core.config import config

def greeting_for(name: str) -> str:
    prefix = config.get("greeting_prefix", "Hello")
    return f"{prefix}, {name}!"
```

### 2.2 写模板（`templates/greeting/hello.html`）

```jinja
{% extends "base.html" %}
{% block title %}{{ t('greeting_title') }} - {{ site_title }}{% endblock %}

{% block content %}
  <h1>{{ greeting }}</h1>
  <form method="post" action="/greet">
    {{ csrf_input() }}                {# Core 提供的全局：自动带令牌 #}
    <input type="text" name="name" maxlength="40" required>
    <button type="submit">{{ t('greeting_submit') }}</button>
  </form>
{% endblock %}
```

### 2.3 写路由（`modules/greeting/routes.py`）

```python
from ...core.http import html, redirect
from ...core.templating import render_template
from . import logic

def register(router):
    @router.route("/hello", methods=["GET"])
    def hello(request):
        name = request.arg("name") or "world"
        return html(render_template("greeting/hello.html", {
            "greeting": logic.greeting_for(name),
        }))

    @router.route("/greet", methods=["POST"])
    def greet(request):
        name = request.form.get("name", "").strip()
        return redirect(f"/hello?name={name}")
```

注意这个 `POST` 路由里**没有一行 CSRF 代码**——调度器默认就会校验。

### 2.4 注册到装配点（`elenvind/app.py`）

装配层是**唯一**允许 import 模块的地方。加一个模块 = 在 `create_app()` 里加一行
（顺序即优先级）：

```python
from .modules.greeting import routes as greeting_routes

def create_app() -> App:
    app = App(...)
    router = app.router
    ...
    greeting_routes.register(router)      # 固定路径必须放在 pages 兜底之前
    ...
```

需要预热内容缓存时，把钩子加进装配层的 `STARTUP_HOOKS`（Core 只提供
`startup(hooks)` 机制，不持有任何注册表）：

```python
from .modules.greeting import logic as greeting_logic

STARTUP_HOOKS = (
    ("Articles loaded", blog_logic.load_articles),
    ("Greetings loaded", greeting_logic.load_all),
)
```

启动钩子抛异常会让**启动失败**。这是刻意的：宁可拒绝启动，
也不要跑一个数据永远为空的站点。

**模块之间需要协作时不要互相 import**：把数据源作为参数从装配层传进去。

```python
# 装配层：显式注入
seo_routes.register(router, articles=blog_logic.sitemap_articles)

# seo 模块：接收并使用，不知道数据来自哪个模块
def register(router, *, articles):
    @router.route("/sitemap.xml", methods=["GET"])
    def sitemap(request):
        ...for slug, date in articles(): ...
```

---

## 3. 路由声明即安全

```python
@route("/public",   methods=["GET"])                                  # 任何人
@route("/settings", methods=["GET", "POST"], auth="required")          # 需登录
@route("/admin",    methods=["GET"], auth="required", permission="admin")
```

| 声明 | Core 自动施加的行为 |
|---|---|
| `methods=["POST"]` | 非安全方法默认校验 CSRF（失败 → 400） |
| `auth="required"` | 未登录：**GET → 302 跳登录页并带回跳地址**；非 GET → 403 |
| `permission="admin"` | 已登录但权限不足 → 403；未登录的 GET → 同上的友好跳转 |
| `fallback=True` | 兜底路由：仅当没有声明路由匹配时才参与（如 `/<slug>`） |
| 任何路由 | 响应自动带全套安全响应头；Cookie 自动 HttpOnly+SameSite=Lax+Secure |
| 任何请求 | 请求体上限、`Content-Type` 白名单、`Content-Length` framing 已在 Core 校验 |

**失效关闭**：`permission` 值必须是已知档位（`public` / `authenticated` / `admin`）。
写错成 `permission="amin"` 会**拒绝**，不会"不认识就放行"。

### 未登录时的用户体验（重要）

直接甩一个 403 是很差的体验。Core 的策略是：

| 场景 | 行为 | 理由 |
|---|---|---|
| 匿名 **GET** 受保护页面 | `302 → /login?next=<原路径>` | 浏览器导航，用户能"登录后继续" |
| 匿名 **POST** / PUT / DELETE | `403` | 表单提交没有"跳转"可言，重定向反而掩盖问题 |
| **已登录**但权限不足 | `403` | 重新登录还是同一个身份，跳登录页毫无意义 |
| 走 `/login?next=…` 登录成功 | `302 → next`（只接受站内路径） | 闭环：从哪来、回哪去 |

`next` 的取值由 `modules/auth/routes.py:safe_next()` 规范化：
只接受以 `/` 开头、不以 `//` 开头、不含 `\` 与 CR/LF 的路径，否则回落到首页。

**想让页面在未登录时展示友好内容而不是跳转**，就把该路径的 GET 拆成 public 路由，
自己渲染提示页（`/user` 就是这么做的）：

```python
@router.route("/user", methods=["GET"])                    # 公开：友好提示页
def profile_page(request):
    if request.user is None:
        return html(render_template("users/profile.html", {...}))   # 请先登录
    return html(_render_profile(request, request.user, "", "error"))

@router.route("/user", methods=["POST"], auth="required")   # 写操作仍受保护
def profile_update(request): ...
```

注意提示页里**只能**放登录入口，绝不能渲染任何账号数据或私有表单
（`tests/test_core_contract.py::test_friendly_public_pages_leak_nothing` 会检查这一点）。

### 状态变更绝不放在 GET 里

友好不等于给 GET 加副作用。典型例子是**退出登录**：

| 请求 | 行为 | 为什么 |
|---|---|---|
| `GET /logout`（已登录） | 200 确认页（含 `csrf_input()` 的 POST 表单） | 友好，且**不**改变状态 |
| `GET /logout`（未登录） | 200 "你已退出登录" | 会话已失效时也有合理页面 |
| `POST /logout` + CSRF | 302 → `/`，清会话 Cookie | 唯一真正退出的路径 |
| `GET /logout` 之外的跨站触发 | 无效 | GET 无副作用，`<img src="/logout">` 什么也做不了 |

如果让 `GET /logout` 直接销毁会话，任何人都能用一张图片把你踢下线
（CSRF 的经典形态）。所以这里的做法是：**GET 只渲染确认，POST 才执行**。

原则：**任何会改变状态的路径（创建 / 更新 / 删除 / 登录 / 退出）
都必须由非安全方法 + CSRF 触发**；GET 只读。

### 权限的三档语义

| 值 | 含义 | 判定 |
|---|---|---|
| `public`（默认） | 任何人 | 恒真 |
| `authenticated` / `required` | 已登录 | `request.user is not None` |
| `admin` | 管理员 | 用户 id == `config.toml` 的 `admin_user_id` |
| 其它任何值 | **拒绝** | 失效关闭 |

"资源属于你"这类**业务权限**不在这三档里，由模块显式调用
`core.auth.require_owner(user, row)` 判断（例如"只有评论作者能删自己的评论"）。

---

## 4. 可以用什么，不可以用什么

### 4.1 允许

```python
from ...core.http import html, text, redirect, Response
from ...core.http import BadRequest, Forbidden, NotFound, PayloadTooLarge
from ...core.templating import render_template
from ...core.markdown import render_markdown
from ...core.session import current_user
from ...core.auth import require_owner, is_admin_user
from ...core.config import config
from ...core.db_base import connect, write_tx, IntegrityError
from ...core.security import hash_password, verify_password      # 使用原语，OK
```

抛 `HttpError` 家族异常即可得到对应状态码（调度器统一翻译成响应）：

```python
if row is None:
    raise NotFound("Article not found")
```

### 4.2 禁止（有静态守卫测试）

| 禁止 | 原因 | 正确做法 |
|---|---|---|
| `SimpleCookie()` / 手拼 `Set-Cookie` | Cookie 属性会漏 | 见下方"Cookie 的唯一出口" |
| `Response.set_cookie()` / `.delete_cookie()` | 模块不该知道 Cookie 名与属性策略 | 会话用 `request.invalidate_session_cookie()`；偏好用 `request.set_preference()` |
| `from ...core.security import SESSION_COOKIE` | import 了它说明你想自己操作那个 Cookie | 同上（静态守卫会拦） |
| 自定义 `hash_password` / `verify_password` | 密码逻辑只能有一份 | `core.security` |
| 自定义 CSRF 校验 | 会出现"某条路径忘了校验" | 什么都不做，调度器默认施加 |
| `get_connection()` | 会用完不关（连接泄漏） | 读用 `with connect()`；写用 `with write_tx()`；不要自己开连接 |
| 自己 `import sqlite3` / 自己 `BEGIN`+`COMMIT` | 绕过 C0 写协调；异常路径容易漏掉回滚/解锁 | `with write_tx() as conn:` |
| 在 `write_tx()` 块内调用另一个写函数（如 `prune()`） | 嵌套取锁：会触发重入守卫，否则就是自死锁 | 移到块外顺序调用 |
| `Environment(...)` / `FileSystemLoader(...)` | 模板环境必须单例 | `render_template()` |
| `markdown.markdown(...)` | 绕过净化 = 存储型 XSS | `render_markdown()` |
| 手写 `<script>` / `on*=` 等标记 | 绕过模板转义 | 写进模板文件 |
| `from ..modules import ...`（在 core 里） | 依赖方向反转 | 用启动钩子 |
| 在日志里带密码 / 令牌 / Cookie / 请求体 / 查询串 | 凭据与 PII 落盘，日志会被备份、被转发 | 只记 id / ip / slug 这类非敏感字段（见下） |
| 自己写访问日志 | 每请求一行、格式统一、且要处理路径里的控制字符（防伪造日志行） | 什么都不做，Core 已经记了 |

#### 日志怎么记

```python
import logging
logger = logging.getLogger(__name__)          # 每条日志自动带模块名

logger.info("Comment created: slug=%s user_id=%s", slug, user["id"])
logger.warning("Comment rejected (too deep): user_id=%s slug=%s", user["id"], slug)
```

- **Core 负责访问日志与写事务日志**：每个请求一行 `Request handled: …`，
  每笔写事务一行（debug 级）；模块不需要自己记这两类；
- **模块负责"业务事件"**：改状态成功（发表、审核、改密、删号…）记 INFO，
  被拒绝/限流/安全信号记 WARNING —— 现有模块都遵循这个划分；
- **绝不放敏感数据进日志**：密码、哈希、会话/CSRF 令牌、Cookie、请求体、
  查询串、邮箱（PII）一律不记。`tests/test_logging_proxy.py` 会跑真实流程
  做脱敏断言，`tests/test_runtime_logging.py` 会断言审计事件**存在**且不含秘密。

#### Cookie 的唯一出口

Cookie 由 Core 在响应收尾阶段统一下发（`Request.pending_cookies()`），
模块只表达"我想要什么"，不碰 `Set-Cookie`：

| 需求 | 模块写什么 | Core 做什么 |
|---|---|---|
| 让当前会话失效（登出 / 改密 / 删号） | `request.invalidate_session_cookie()` | 删服务端会话 + 下发 `Max-Age=0`（含 `__Host-` 前缀兼容名） |
| 记住一个站内偏好（如主题） | `request.set_preference("theme", "dark")` | 按 `core.security.PREFERENCE_COOKIES` 白名单校验后下发 |
| 读一个站内偏好 | `request.preference("theme")` | 白名单校验后返回，非法值给 `None` |

好处是 Cookie 名、有效期、`HttpOnly` / `SameSite` / `Secure` / 前缀策略
全项目只有一处定义；切换 `cookie_prefix` 时模块一行都不用改。

### 4.3 关于 `markupsafe.Markup`

`Markup` 表示"这段 HTML 已由 Core 净化，可安全输出"。
模块需要输出富文本时，只能从 `render_markdown()` 拿返回值；
不要在业务里自己构造 `Markup` 包装用户输入。

---

## 5. 模板约定

- 所有模板 `{% extends "base.html" %}`；`base.html` 已包含 `<head>`、
  顶栏、页脚、`lang`、`data-theme`。
- Core 自动注入的上下文变量：`request` / `user` / `csrf_token` / `lang` /
  `site_title` / `description` / `keywords` / `theme` / `admin_badge` /
  `copyright_name` / `current_year` / `user_count` / `nav_items` /
  `social_items` / `project_items`。
- Core 提供的模板全局：`csrf_input()` / `t(key, **kw)` / `static_url(kind)` /
  `is_admin_user(user)` / `theme_icons`；过滤器 `date` / `datetime`。
- 表单里必须写 `{{ csrf_input() }}`（否则提交会被 400 拒绝）。
- 自动转义默认开启。**不要为了省事加 `|safe`**：需要富文本就用
  `render_markdown()` 的返回值。

---

## 6. 内容格式（文章与页面）

文章放在 `articles/`，扩展名 `.md`，开头是 TOML front matter：

```markdown
+++
title = "文章标题"                    # 必填
date = 2026-09-07T10:00:00+08:00     # 用于首页排序（倒序）
lastmod = 2026-10-01T09:00:00+08:00  # 可选
authors = ["Hao Wu"]                 # 可选
tags = ["python", "web"]             # 可选
summary = "一句话摘要"                # 可选
+++

# 正文标题

这里是**标准 Markdown**。
```

自定义页面放在 `custom_pages/`，**不要** front matter，文件名即路由
（`about.md` → `/about`）。

### 视频指令 `@video(url)`

Markdown 没有原生视频语法，而正文里的**原始 HTML 会被转义**（安全默认值），
所以这里提供一个显式指令，**单独占一行**：

```markdown
@video(https://example.com/movie.mp4)
@video(/imgs/local-clip.mp4)
```

渲染结果（默认带原生控制条）：

```html
<video src="https://example.com/movie.mp4" controls preload="metadata"></video>
```

规则：

| 行为 | 说明 |
|---|---|
| 默认属性 | `controls`（必有）+ `preload="metadata"`（避免整段视频被预下载） |
| 允许的地址 | `http` / `https` / 站内相对路径（`/imgs/x.mp4`） |
| 拒绝的地址 | `javascript:` / `data:` / `vbscript:` / `file:`，以及含引号、尖括号、`&`、反引号、反斜杠的地址 |
| 拒绝时的表现 | **原样显示成文字**，方便一眼看出写错了（不静默丢弃） |
| 代码围栏 / 行内代码内 | 不解析，保持原文 |
| 行中间 | 不解析（避免误伤散文里的 `@video(...)` 写法） |

实现位置：`core/markdown.py` 的 `VideoDirectiveExtension`。
它用"预处理器换占位符 → 后处理器替换成标签"两步，先经 `safe_media_url`
校验，产出再统一过白名单净化器。**不要**绕过这个入口去手写 `<video>` 标签。

正文渲染经过 Core 的白名单净化：原始 HTML 会显示为可见文本，
`javascript:` / `data:` / `vbscript:` / `file:` 协议会被丢弃。
因此写内容不需要考虑 XSS。

---

## 7. 数据访问（读 `connect()`，写 `write_tx()`）

```python
from ...core.db_base import connect, write_tx

def count_rows() -> int:
    with connect() as conn:                       # 只读：不加锁
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]

def rename(row_id: int, name: str) -> None:
    with write_tx() as conn:                      # 写：跨进程锁 + 事务 + 提交/回滚
        conn.execute("UPDATE t SET name = ? WHERE id = ?", (name, row_id))
```

### 7.1 两条入口的职责

> 本节是**模块作者需要知道的全部**。完整契约（锁文件规则、保证与不保证、
> 迁移机制、可观测性、读-判断-写边界）见
> [数据库与 C0 写协调](database.md)。

| 入口 | 用途 | 事务/锁 |
|---|---|---|
| `connect()` | **只读**查询（SELECT） | 不加锁；`with conn:` 只在 DML 时才有隐式事务 |
| `write_tx()` | **所有**写事务（INSERT/UPDATE/DELETE/REPLACE/DDL） | `flock` 跨进程排他 + `BEGIN IMMEDIATE` + commit/rollback + close |

- `write_tx()` 是**整个项目唯一正式的写事务入口**，也是唯一允许开写事务的地方；
- `write_tx()` **不接收 SQL**：它不是 executor，业务 SQL 仍写在各自的 `core/db_*.py` 里；
- **读-判断-写**必须整体放进**同一个** `write_tx()`（见 `db_comment_rate.try_post_comment`、
  `db_session.get_session_user`），否则判断依据可能在两者之间被别人改掉（TOCTOU）；
- `write_tx()` **不可嵌套**：同一线程里再进一次会立刻抛 `RuntimeError`。所以
  "写完之后顺手清理"这类动作（`core.db_prune.prune()`）必须在块**外**调用；
- 需要关联的异常类型（如 `IntegrityError`）请从 `core.db_base` 取，
  不要 `import sqlite3`（有静态守卫）。
- 用户行是 `sqlite3.Row`：**不支持 `getattr`**，只能用 `row["col"]`。
- 新增表/列时改 `core/db_base.py` 的 `SCHEMA_VERSION` 与 `_MIGRATIONS`；
  迁移与初始化本身也在 `write_tx()` 之内，不需要（也不该）自己 `BEGIN`。

### 7.2 C0 写协调到底保证什么、不保证什么

**保证（守协议的进程之间）**：

- Gunicorn 多 worker 之间**跨进程排他互斥**：同一时刻只有一个进程在写；
- 锁覆盖**整个**事务（取锁 → `BEGIN IMMEDIATE` → 业务 SQL → commit/rollback → 关连接 → 解锁），
  不会只锁住 `BEGIN` 那一瞬间；
- SQLite 自身事务继续提供**事务原子性**；`busy_timeout` 保留为 SQLite 层兜底；
- WAL 保留（由初始化路径确立）：读写可以并发，读不需要排队等写锁；
- 锁文件独立于数据库（`sqlite.db` → `sqlite.db.write.lock`），路径由当前 DB 路径动态派生，
  进程被 `SIGKILL` 后由内核在 fd 关闭时释放锁。

**不保证**：

- **不保证严格 FIFO**：`flock` 只提供排他互斥，竞争进程阻塞等待，先来后到不保证；
- **不保证"零数据损坏"**：它降低的是并发写风险，不是形式化证明；
- **不自动约束协议外的程序**：别的工具/脚本直接用 `sqlite3` 写同一个库时，不会遵守这把应用锁
  （SQLite 自己的锁仍会生效，`busy_timeout` 之内会等待，超时会报 `database is locked`）；
- **不支持多机共享**：数据库与锁文件必须位于**本地文件系统**（NFS/网络盘上的 flock 语义
  不可依赖）。需要多机部署时换 PostgreSQL，而不是继续叠加 SQLite 锁机制；
- WAL 允许并发读写，但**不代表没有 checkpoint 相关的阻塞**。

### 7.3 可观测性

获取锁的等待时长会记录：正常情况是 `DEBUG` 一行（`Write lock acquired in …ms`），
等待超过 1 秒（`_SLOW_LOCK_WAIT_SECONDS`）会升级为 `WARNING`
（`Write lock wait …ms`）。想回答"写锁是不是已经成为瓶颈"，盯这条 WARNING 就够了 ——
不需要额外的锁监控进程或线程。

---

## 8. 国际化

`i18n/{en,zh,ja}.toml` 是文案表。模板里 `{{ t('key', name=value) }}`；
Python 里 `from ...core.i18n import t` + `t(current_lang(), 'key', ...)`。

新增文案三份语言都要加，否则 `t()` 会回落到英文/键名。

---

## 9. 测试

```bash
python -m unittest discover -s tests -t .           # 全部
python -m unittest tests.test_core_contract -v      # Core Contract（安全能力不可绕过）
python -m unittest tests.test_architecture -v       # 分层依赖方向（本指南第 1 节）
python smoke_driver.py                              # 真起 Gunicorn 跑 61 项检查（在项目根）
```

写模块测试时继承 `tests.support.ElenvindTestCase`：它提供临时数据库、
临时内容目录、WSGI 夹具与登录辅助（`write_article` / `login_ok` / `fetch_csrf`）。
夹具是 `AppHarness`：它自己拼 `environ`、自己收 `start_response`，因此不需要
HTTP 服务器就能覆盖完整链路（`build_environ()` / `call_wsgi()` 可用于驱动
独立的 `App` 实例，`StreamInput` 可模拟短读与截断的请求体）。

**两套守卫会自动检查你的模块**：

| 测试 | 检查什么 |
|---|---|
| `tests/test_core_contract.py` | 遍历 `modules/**/*.py`：不得重造安全轮子、不得执行 SQL、不得开数据库连接、写事务必须走 `write_tx()` |
| `tests/test_architecture.py` | Core 不 import 模块、模块不 import 装配层、**模块之间零 import**、无循环依赖、无旧 `features/` 路径、模块公开入口形状 |

---

## 10. 常见错误与对应现象

| 现象 | 原因 |
|---|---|
| 表单提交总是 400 | 模板里漏了 `{{ csrf_input() }}` |
| `TypeError: 'NoneType' object is not subscriptable` | 忘了声明 `auth="required"`，`request.user` 是 None |
| `TypeError: 'sqlite3.Row' object is not subscriptable` | 对 Row 用了 `getattr`，改用 `row["col"]` |
| `ResourceWarning: unclosed database` | 用了 `get_connection()`，改用 `with connect()` |
| 页面出现 `&lt;input ...&gt;` 文本 | 忘了让函数返回 `Markup`（Core 内部问题） |
| 模板改动没生效 | 检查 `templates_dir`；环境是单例但 `auto_reload=True` |
| 启动直接失败 | 启动钩子抛异常，见日志里的 `[ERROR] Startup failed` |
