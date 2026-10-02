# Feature 开发指南

> **一句话契约：Feature 负责业务，Web Core 负责 Web 安全。**

写业务代码时你不需要（也不允许）考虑 CSRF、Cookie 属性、安全响应头、
请求体上限、会话轮换、密码哈希。这些都已在 Core 里实现一次，
并由 `tests/test_core_contract.py` 静态 + 运行时双重守卫。

---

## 1. 目录结构

```
elenvind/
  core/                 Web Core：与业务无关的 Web 安全与基础设施
    app.py              ASGI 入口与请求流水线编排
    routing.py          @route 声明 + 调度器（CSRF / 认证 / 权限闸门）
    http.py             Request / Response / 安全头 / Cookie 下发
    security.py         密码哈希、Cookie 构造、CSP 与安全头定义
    session.py          服务端会话（轮换、两级过期、清理）
    auth.py             认证与授权（verify_credentials / check_permission）
    csrf.py             CSRF 单点实现 + 模板全局 csrf_input()
    templating.py       Jinja2 唯一入口 render_template()
    markdown.py         Markdown 唯一入口 render_markdown() + 白名单净化
    db_base.py          连接入口 connect() / 迁移
    db_*.py             各表的数据访问
    config.py           配置加载与校验
    context.py          请求级上下文（模板全局取当前请求）
    lifespan.py         启动/关闭流程 + Feature 启动钩子
    i18n.py             文案表
  features/             业务：只依赖 core
    registry.py         唯一装配点（Core 不认识任何 Feature）
    blog/               文章索引、正文渲染、评论
    pages/              自定义页面
    auth/               登录 / 注册 / 登出
    users/              个人中心
    admin/              管理员页面
    seo/                robots.txt / sitemap.xml
    system/             404 / 403 / 500 页面
  templates/            Jinja2 模板（Python 只准备数据，HTML 全在这里）
```

依赖方向**单向**：`features/` → `core/` → 标准库 + Uvicorn + Jinja2 + Markdown。
Core 里不允许出现任何 Feature 的名字（有守卫测试）。

---

## 2. 五分钟写一个 Feature

### 2.1 写逻辑（`features/greeting/logic.py`）

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

### 2.3 写路由（`features/greeting/routes.py`）

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

### 2.4 注册到装配点（`features/registry.py`）

```python
from .greeting import routes as greeting_routes

def install(router) -> None:
    ...
    greeting_routes.register(router)      # 固定路径放在 pages 兜底之前
```

如果有内容缓存需要预热：

```python
def register_startup_hooks() -> None:
    from ..core.lifespan import register_startup_hook
    register_startup_hook("Greetings loaded", greeting_logic.load_all)
```

启动钩子抛异常会让**启动失败**。这是刻意的：宁可拒绝启动，
也不要跑一个数据永远为空的站点。

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

`next` 的取值由 `features/auth/routes.py:safe_next()` 规范化：
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

"资源属于你"这类**业务权限**不在这三档里，由 Feature 显式调用
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
from ...core.db_base import connect
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
| `Response.set_cookie()` / `.delete_cookie()` | Feature 不该知道 Cookie 名与属性策略 | 会话用 `request.invalidate_session_cookie()`；偏好用 `request.set_preference()` |
| `from ...core.security import SESSION_COOKIE` | import 了它说明你想自己操作那个 Cookie | 同上（静态守卫会拦） |
| 自定义 `hash_password` / `verify_password` | 密码逻辑只能有一份 | `core.security` |
| 自定义 CSRF 校验 | 会出现"某条路径忘了校验" | 什么都不做，调度器默认施加 |
| `get_connection()` | 会用完不关（连接泄漏） | `with connect() as conn:` |
| `Environment(...)` / `FileSystemLoader(...)` | 模板环境必须单例 | `render_template()` |
| `markdown.markdown(...)` | 绕过净化 = 存储型 XSS | `render_markdown()` |
| 手写 `<script>` / `on*=` 等标记 | 绕过模板转义 | 写进模板文件 |
| `from ..features import ...`（在 core 里） | 依赖方向反转 | 用启动钩子 |

#### Cookie 的唯一出口

Cookie 由 Core 在响应收尾阶段统一下发（`Request.pending_cookies()`），
Feature 只表达"我想要什么"，不碰 `Set-Cookie`：

| 需求 | Feature 写什么 | Core 做什么 |
|---|---|---|
| 让当前会话失效（登出 / 改密 / 删号） | `request.invalidate_session_cookie()` | 删服务端会话 + 下发 `Max-Age=0`（含 `__Host-` 前缀兼容名） |
| 记住一个站内偏好（如主题） | `request.set_preference("theme", "dark")` | 按 `core.security.PREFERENCE_COOKIES` 白名单校验后下发 |
| 读一个站内偏好 | `request.preference("theme")` | 白名单校验后返回，非法值给 `None` |

好处是 Cookie 名、有效期、`HttpOnly` / `SameSite` / `Secure` / 前缀策略
全项目只有一处定义；切换 `cookie_prefix` 时 Feature 一行都不用改。

### 4.3 关于 `markupsafe.Markup`

`Markup` 表示"这段 HTML 已由 Core 净化，可安全输出"。
Feature 需要输出富文本时，只能从 `render_markdown()` 拿返回值；
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

## 7. 数据访问

```python
from ...core.db_base import connect

def count_rows() -> int:
    with connect() as conn:                       # 提交 / 回滚 / 关闭三合一
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
```

- `connect()` 是**唯一**开连接方式。不要用 `sqlite3.connect()` 或 `get_connection()`。
- 需要"读-判断-写"原子性时用 `BEGIN IMMEDIATE`（见 `core/db_comment_rate.py`）。
- 用户行是 `sqlite3.Row`：**不支持 `getattr`**，只能用 `row["col"]`。
- 新增表/列时改 `core/db_base.py` 的 `SCHEMA_VERSION` 与 `_MIGRATIONS`。

---

## 8. 国际化

`i18n/{en,zh,ja}.toml` 是文案表。模板里 `{{ t('key', name=value) }}`；
Python 里 `from ...core.i18n import t` + `t(current_lang(), 'key', ...)`。

新增文案三份语言都要加，否则 `t()` 会回落到英文/键名。

---

## 9. 测试

```bash
python -m unittest discover -s tests -t .      # 全部
python -m unittest tests.test_core_contract -v # 契约守卫
python smoke_driver.py                         # 真起服务跑 61 项检查（在项目根）
```

写 Feature 测试时继承 `tests.support.ElenvindTestCase`：它提供临时数据库、
临时内容目录、ASGI 夹具与登录辅助（`write_article` / `login_ok` / `fetch_csrf`）。

**契约测试会自动检查你的 Feature**：`tests/test_core_contract.py` 会遍历
`features/**/*.py`，所以新代码一旦重造安全轮子会立刻失败。

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
