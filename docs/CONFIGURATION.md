# 配置参考（config.toml）

`config.toml` 是**唯一**的配置来源，只在启动时读取一次：

- 所有键必须能通过 `core/config.py:validate_config()`，非法值**直接拒绝启动**
  （不会先跑起来、再在某个页面 500）；
- 路径类配置一律相对**项目根目录**解析，与启动时的 cwd 无关；
- 文件不存在 → 启动失败。复制模板：`cp config.example.toml config.toml`；
- TOML 的表（`[table]`）会"吃掉"它之后的所有顶层键，因此**所有表都写在顶层键之后**。

启动时的告警（不阻止启动，但要处理）：

| 日志 | 含义 |
|---|---|
| `Listening on 0.0.0.0 while trusting forwarded headers …` | 端口可被直连，绕过 TLS/HSTS/边缘限速。改 `host = "127.0.0.1"`，或给端口加防火墙 |
| `admin_user_id=N does not match an active user` | 配置的管理员不存在/已注销 → 没人能涂黑评论 |
| `No administrator configured` | `admin_user_id` 缺失、为 `null` 或非法 |

---

## 顶层：站点与内容

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `locale` | string | `en` | 界面语言，只能是 `en` / `zh` / `ja`（也接受 `zh-CN` 这类带区域后缀的写法）。不做浏览器自动检测 |
| `title` | string | 必填 | 站名（非空） |
| `copyright` | string | 回落到 `title` | 页脚版权署名 |
| `keywords` | string / list | `""` | `<meta name="keywords">`，列表会以 `, ` 连接 |
| `description` | string | `""` | `<meta name="description">` |
| `site_url` | string | `""` | **绝对地址的唯一来源**（robots.txt / sitemap.xml）。必须是不带结尾斜杠的 `http(s)://…`；留空则不输出绝对地址。绝不从请求的 Host 头推导 |
| `admin_user_id` | int / `null` | `1` | 管理员账号 id：可涂黑任意评论。`null` = 无管理员；非法值拒绝启动（绝不静默回退到 1） |
| `admin_badge` | string | `BIG BOSS` | 管理员昵称旁的徽章文字；空串 = 不显示 |
| `deleted_user_nickname` | string | `Journeyed On` | 已注销用户的评论显示名 |
| `max_length` | int | `1000` | 单条评论最大字符数（前端 `maxlength` 与后端双重限制） |
| `max_comment_depth` | int | `32` | 评论最大层级（顶层 = 1）。**硬约束**：写入侧在事务内校验，超层回复被拒 |
| `max_comments_per_article` | int | `1000` | 单篇文章评论数量上限（只统计**未涂黑**的评论；涂黑旧评论即可释放名额） |
| `registration_enabled` | bool | `true` | 是否开放公开注册 |
| `session_absolute_days` | int | `30` | 绝对过期：会话创建后活够这么多天必须重新登录（token 泄露的风险窗口硬上限）。`0` = 该维度不过期 |
| `session_idle_days` | int | `15` | 滑动过期：闲置这么多天即失效；有效访问会刷新 `last_seen`（刷新按 5 分钟节流）。`0` = 不过期 |
| `max_body_size` | int | `1048576` | POST 表单体积上限（字节），超出返回 413 |
| `database` | string | `sqlite.db` | SQLite 文件路径；环境变量 `ELENVIND_DB` 优先级更高（多环境隔离用） |
| `articles_dir` | string | `articles` | 文章目录（`*.md`，TOML front matter） |
| `custom_pages_dir` | string | `custom_pages` | 自定义页面目录（`*.md`，文件名即路由） |
| `templates_dir` | string | `elenvind/templates` | Jinja2 模板目录；只有整体替换模板时才需要改 |
| `use_builtin_css` | bool | `true` | `[static].css` 为空时是否使用内置样式表（`/css/style.css`）。`false` 则完全不输出 `<link>` |

## `[server]`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `host` | string | `127.0.0.1` | 监听地址。**保持回环**，公网服务由 Nginx 回源；`python run.py --bind` 可临时覆盖 |
| `port` | int | `6789` | 监听端口 |
| `workers` | int | `2` | Gunicorn worker 数（1–64）。写事务是串行的（flock + BEGIN IMMEDIATE），加 worker 只对读有帮助 |
| `trusted_proxies` | list | `["127.0.0.1", "::1"]` | 受信代理（**唯一来源**）：仅当直连对端在此列表中时，才采信 `X-Forwarded-For`（客户端 IP）与 `X-Forwarded-Proto`（https 判定）。多级代理请把每一跳都写进来 |
| `cookie_prefix` | bool | `false` | 给会话/CSRF Cookie 加 `__Host-` 前缀（要求全程 HTTPS + `Path=/` + 无 Domain）。站点确定只能 HTTPS 访问时开启 |

## `[pagination]`

| 键 | 默认 | 说明 |
|---|---|---|
| `per_page` | `6` | 首页每页文章数 |

## 评论与登录限流

`[comment_limits]`

| 键 | 默认 | 说明 |
|---|---|---|
| `max_per_user` | `5` | 窗口内单用户最多评论数 |
| `max_per_ip` | `10` | 窗口内单 IP 最多评论数 |
| `window_seconds` | `60` | 窗口长度（秒） |

`[login_limits]`：三闸门 + 渐进冷却（第 `limit` 次失败起冷却 `base=60s`，
此后每多一次翻倍，上限 24h）。

| 键 | 默认 | 说明 |
|---|---|---|
| `max_email_failures` | `5` | 单邮箱失败上限（主防线，防定向爆破） |
| `email_window_seconds` | `86400` | 单邮箱统计窗口 |
| `max_ip_failures` | `20` | 单 IP 失败上限（辅助防线） |
| `ip_window_seconds` | `900` | 单 IP 统计窗口 |
| `max_global_failures` | `200` | 全站失败上限（分布式爆破的最后闸门） |
| `global_window_seconds` | `900` | 全站统计窗口 |

> **闸门只约束错误凭据**：闸门命中时请求仍会走密码校验，密码正确即放行并清空该
> 邮箱的失败流水（等于当场自解封）；密码错误才返回冷却提示，并补记这次失败。
> 因此不存在"被限流锁死、正确密码也进不来"的状态。回归测试见
> `tests/test_auth_lockout.py`。

`[register_limits]`

| 键 | 默认 | 说明 |
|---|---|---|
| `max_per_ip` | `5` | 窗口内单 IP 最多注册尝试次数 |
| `window_seconds` | `3600` | 窗口长度（秒） |

## `[static]`

| 键 | 默认 | 说明 |
|---|---|---|
| `css` | `""` | 全站样式表地址。留空 → 由 `use_builtin_css` 决定；填值 → 用它（站内路径或 CDN 绝对 URL，由**你的** Web 服务器提供） |
| `favicon` | `""` | 站点图标；留空回落到 `elenvind/static/imgs/favicon.ico\|png` |
| `logo` | `""` | 页头站标（与站名并排）；留空依次回退到配置的 `favicon` → 缺省图标 |
| `hero` | `""` | 首页 Hero 大图；留空则整块 hero 不渲染 |

所有地址都要过统一校验：站内绝对路径或 `http(s)` 绝对 URL；禁止引号、圆括号、
尖括号、反斜杠、空白与控制字符（它们会进入 HTML 属性甚至内联 CSS 的 `url()`），
也禁止协议相对地址（`//host/…`）。

## `[params]`

页头/页脚与首页的展示内容。`url` / `icon` 与 `[static]` 走同一套 URL 校验，
社交链接额外允许 `mailto:`。

```toml
[params]
intro = "首页一句话介绍"

[[params.nav]]           # 页脚导航
name = "About"
url = "/about"

[[params.social]]        # 首页社交链接（icon 留空则显示名称文字）
name = "Codeberg"
url = "https://codeberg.org/"
icon = "/imgs/codeberg.svg"

[[params.projects]]      # 首页项目列表
name = "Elenvind"
url = "https://github.com/ortzikantu/elenvind"
description = "一句话说明"
```

## `[logging]`

| 键 | 默认 | 说明 |
|---|---|---|
| `level` | `info` | `debug` / `info` / `warning` / `error` / `critical` |
| `file` | `logs/app.log` | 日志文件（相对项目根目录） |
| `rotate` | `false` | `false`（推荐）：只写当前文件，由外部 `logrotate` 轮转（多 worker 下唯一安全的做法，见 `deploy/logrotate.conf`）。`true`：应用内轮转，仅在**单 worker**或本地开发时使用 |
| `max_bytes` | `10485760` | 仅 `rotate = true` 时生效 |
| `backup_count` | `5` | 仅 `rotate = true` 时生效 |

## `[security]`

安全响应头由 Core 在响应收尾时统一注入，模块层不感知。

| 键 | 默认 | 说明 |
|---|---|---|
| `csp_enabled` | `true` | `false` = 完全不下发 CSP（不推荐） |
| `permissions_policy` | 内置全关 | 留空字符串 = 不下发该头；删掉整行 = 用内置默认（关得更全） |
| `hsts_enabled` | `true` | 是否下发 HSTS。**只有 HTTPS 请求**才会真的带上；若 `cookie_prefix = true` 而这里留空，按启用处理 |
| `hsts_max_age` | `31536000` | 秒（0–63072000）。首次上线建议先用 300 确认无误 |
| `hsts_include_subdomains` | `false` | 子域全部支持 HTTPS 后再开 |

### `[security.csp]`

只写要改的指令，其余用内置默认：

| 默认指令 | 值 |
|---|---|
| `default-src` | `'self'` |
| `script-src` | `'none'`（本站 Zero-JS） |
| `style-src` | `'self' 'unsafe-inline'`（首页 hero 的内联 `style=` 需要，**别删**） |
| `img-src` | `'self' data: http: https:` |
| `media-src` | `'self' http: https:` |
| `font-src` | `'self'` |
| `connect-src` | `'self'` |
| `object-src` | `'none'` |
| `base-uri` | `'none'` |
| `form-action` | `'self'` |
| `frame-ancestors` | `'none'` |

**只允许收窄**：显式写裸 `*`、`http:`、`https:` 会被启动校验拒绝（那等于把"任意
第三方"重新放开）。要放行外部资源就写明确的主机：

```toml
[security.csp]
img-src = ["'self'", "data:", "https://cdn.example.com"]
media-src = ["'self'", "https://videos.example.com"]
# 字符串形式也行：style-src = "'self' 'unsafe-inline'"
# 整条关掉：object-src = false
```
