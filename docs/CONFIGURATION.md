# 配置文件使用指南（config.toml）

配置文件位于项目根目录的 `config.toml`（模板见 `config.example.toml`）。
配置只在**启动时加载并校验一次**（`run.py` 与 lifespan 阶段各校验一次，
不合法直接拒绝启动），修改后需重启服务生效；
文章与自定义页面不在此列（见《运维使用指南》的热更新说明）。

**路径类配置一律相对项目根目录解析**，不受启动时工作目录（cwd）影响。

## 顶层键

| 键 | 默认值 | 说明 |
|---|---|---|
| `locale` | `"en"` | **站点界面语言（三选一）**：`"en"` / `"zh"` / `"ja"`。影响 `<html lang>` 与全站文案；带区域后缀如 `"zh-CN"` 会归一为主语言，其它值**启动失败**。 |
| `title` | — | 站点名称（必填）。显示在浏览器标题与页头站名。 |
| `site_url` | — | **站点对外地址**，robots.txt / sitemap.xml 里绝对 URL 的**唯一来源**。必须是不带结尾斜杠的 http(s) 绝对地址；留空则这些文件不输出绝对地址。**绝不从请求的 Host 头推导**（防 Host 头投毒）。 |
| `copyright` | 站点名 | 页脚版权署名，显示为 `© 2026 {copyright}`。留空或未设置时**回落到站点名 `title`**（再回落到 `"Elenvind"`），不会显示空值或 `None`。 |
| `keywords` | — | SEO `<meta keywords>`；留空不输出。兼容纯字符串 `"k1, k2"` 或数组 `["k1", "k2"]`。 |
| `description` | — | SEO `<meta description>`；留空不输出。 |
| `admin_user_id` | `1` | 管理员账号 id。缺失 -> `1`；显式 `null` -> 无管理员；非法值拒绝启动。详见[管理员判定](#管理员判定-admin_user_id)。 |
| `admin_badge` | `"BIG BOSS"` | 管理员徽章文字；**留空 `""` 则彻底不显示徽章**。 |
| `deleted_user_nickname` | `"Journeyed On"` | 已注销账号的评论昵称占位文案。 |
| `max_length` | `1000` | 单条评论最大字符数（服务端校验与 textarea 双重限制）。 |
| `max_comment_depth` | `32` | 评论最大层级（顶层 = 1）。到顶后页面不再显示回复入口，**写入侧同样拒绝**（`too_deep`）。合法区间 `1–10000`；**`0` 不是"不限层级"，会被拒绝启动**。 |

`max_comment_depth` 是**硬约束**，且判定点在 `core.db_comment_rate.try_post_comment`
的事务内（与 `INSERT` 同一事务）：

- 模板不渲染回复按钮只是**提示**——`reply_to` 是表单字段，可以直接构造；
- 路由层还有一道预检，但它在事务**之外**，属于 TOCTOU（并发时两个请求可能同时
  看到"还没到顶"），因此只作为"给出更友好文案"的快捷路径，不作数；
- 层级计算只有一处实现：`core.db_comment.comment_depth`（渲染侧
  `features/blog/logic.comment_depth` 是它的包装），因此两边不会算出不同结果。

**没有"不限制层级"这个选项**：合法区间是 `1–10000`（`core/config.py` 的
`_check_int(cfg.get("max_comment_depth", 32), ..., 1, 10000)`），设成 `0` 或负数
会在启动校验阶段直接拒绝启动，而不是退化成"无限层级"。
| `max_comments_per_article` | `1000` | 单篇文章评论总数上限，超出返回 429。 |
| `registration_enabled` | `true` | 是否开放公开注册；`false` 时 `/register` 只显示"已关闭注册"。 |
| `session_absolute_days` | `30` | **绝对过期**：会话创建后活够这么多天就必须重新登录，与活跃程度无关。这是 token 泄露后风险窗口的硬上限。 |
| `session_idle_days` | `15` | **滑动过期**：闲置超过这么多天即失效；有效访问会刷新 `last_seen`，因此常用设备不会被踢。 |
| `max_body_size` | `1048576` | POST 请求体上限（字节，1 MB）。超出返回 413，缺 `Content-Length` 返回 411，非表单类型返回 415。 |
| `database` | `"sqlite.db"` | SQLite 文件路径（相对项目根）。**优先级：`ELENVIND_DB` 环境变量 > 本键 > 默认 `<项目根>/sqlite.db`**；本键留空时显式回落到默认路径（不会沿用进程里上一次的取值）。 |
| `articles_dir` | `"articles"` | 文章目录（相对项目根）。 |
| `custom_pages_dir` | `"custom_pages"` | 自定义页面目录（相对项目根）。 |
| `templates_dir` | `"elenvind/templates"` | Jinja2 模板目录（相对项目根）。整站换模板时才需要改。 |
| `use_builtin_css` | `true` | `[static].css` 为空时是否回落到应用缺省样式表。详见 [`[static]`](#static-静态资源地址)。 |

> 密码哈希参数（scrypt 的 N/r/p）**不**通过配置暴露：它属于安全默认值，
> 改了会让新老哈希不一致；升级算法请改 `elenvind/core/security.py` 并保留
> `password_needs_rehash` 的渐进式升级路径。

### 管理员判定（`admin_user_id`）

管理员身份**只有一个来源**：这个键。业务代码一律走 `core.security.is_admin()`
/ `is_admin_id()`，不散落 `id == 1` 这类魔法数字。

| 写法 | 结果 |
|---|---|
| 键缺失（整行删掉） | 取默认值 `1`，即最早注册的那个账号是管理员 |
| `admin_user_id = 1` | 同上（显式） |
| `admin_user_id = null` | **没有管理员**：`is_admin()` 对任何人返回 False |
| `admin_user_id = 0` / 负数 / `"1"` / `3.5` / `true` | **拒绝启动**，错误提示类似 `admin_user_id must be between 1 and 2147483647, got 0` |

把 `0` 设计成拒绝而不是"无管理员"，是为了避免"以为关掉了管理入口、其实配置
根本没生效"这类静默失败：真要点掉管理员请写 `null`，语义明确。

## `[server]` 服务监听

| 键 | 默认 | 说明 |
|---|---|---|
| `host` | `"127.0.0.1"` | 监听地址。默认只监听回环（配合 Nginx 本机回源）。`config.example.toml` 里写的是 `"0.0.0.0"`（面向"直接对外提供服务"的场景），**若用那份模板，请按需改回 `"127.0.0.1"`**。 |
| `port` | `6789` | 监听端口（1–65535，越界启动失败）。 |
| `trusted_proxies` | `["127.0.0.1","::1"]` | **安全相关**：只有直连对端属于该列表时，`X-Forwarded-For` 才会被采信为客户端 IP（登录/评论限流按此计数）。`X-Real-IP` 一律不采信。切勿加入公网地址。 |
| `cookie_prefix` | `false` | 是否给会话/CSRF Cookie 加 `__Host-` 前缀。要求全程 HTTPS + `Path=/` + 无 Domain；仅在站点确定只能通过 HTTPS 访问时开启。开启后读取侧仍兼容旧的无前缀 Cookie，切换不会把所有人踢下线。另外它还会让 `[security].hsts_enabled` 缺失时视为启用。 |

### 代理信任边界（务必理解，涉及两个层次）

应用对"代理"的信任分两处，默认值一致、可分别调整：

1. **HTTP scheme（`https` 判定）**由 uvicorn 负责。`run.py` 以
   `proxy_headers=True` + `forwarded_allow_ips="127.0.0.1"` 启动，
   即**只有回环地址**的 `X-Forwarded-Proto` 会被采信，据此决定
   `Secure` Cookie 与 HSTS 是否下发。
2. **客户端 IP** 由应用负责，规则见 `trusted_proxies`（本表上一行）。

两者必须都能对得上，安全边界才完整：

- 代理在**本机回环**（默认拓扑）：两级都默认生效，无需额外配置。
- 代理在**其它地址**：除了把该地址写进 `trusted_proxies`，
  还必须用 `uvicorn --forwarded-allow-ips="<代理地址>"` 直接启动
  （不要再用 `run.py` 的内置默认值），否则 `X-Forwarded-Proto` 不被采信，
  HTTPS 下 `Secure` Cookie 不会下发。
- **防火墙必须封闭应用端口**：若应用端口可被公网直连，攻击者可以直接伪造
  `X-Forwarded-Proto: https`（uvicorn 会当成真实 scheme）以及绕过全部代理假定。
  这是本应用最重要的部署前置条件。

## `[pagination]` 分页

| 键 | 默认 | 说明 |
|---|---|---|
| `per_page` | `10` | 首页 Writing 区块每页文章数，超出则出现分页导航。 |

## `[comment_limits]` 评论限流

| 键 | 默认 | 说明 |
|---|---|---|
| `max_per_user` | `5` | 窗口内单用户最多评论数，超出 429。 |
| `max_per_ip` | `10` | 窗口内单 IP 最多评论数，超出 429。 |
| `window_seconds` | `60` | 统计窗口。 |

限流判定与评论写入在同一个 SQLite 写事务里完成（`db_comment_rate.try_post_comment`），
不会出现"检查通过但并发插入绕过"的竞态；被限流的请求也不写流水。

## `[login_limits]` 登录限流

| 键 | 默认 | 说明 |
|---|---|---|
| `max_email_failures` | `5` | 单邮箱失败上限（防定向爆破，主防线）。 |
| `email_window_seconds` | `86400` | 邮箱维度窗口（24 小时）。 |
| `max_ip_failures` | `20` | 单 IP 失败上限（辅助防线）。 |
| `ip_window_seconds` | `900` | IP 维度窗口（15 分钟）。 |
| `max_global_failures` | `200` | 全站失败上限（分布式爆破最后闸门）。 |
| `global_window_seconds` | `900` | 全局维度窗口。 |

登录成功后该账号失败流水自动清除。

## `[register_limits]` 注册限流

| 键 | 默认 | 说明 |
|---|---|---|
| `max_per_ip` | `5` | 窗口内单 IP 最多注册尝试次数（含失败），超出显示"注册尝试过于频繁"。 |
| `window_seconds` | `3600` | 统计窗口（1 小时）。 |

## `[static]` 静态资源地址

| 键 | 默认回退 | 说明 |
|---|---|---|
| `css` | 见下方"样式表解析顺序" | 全站样式表 URL。填站内路径或 CDN 绝对地址；**留空则回落到缺省样式表**。 |
| `favicon` | 缺省图标 | 浏览器标签页图标 URL。留空则回落到 `elenvind/static/imgs/favicon.ico`（或 `.png`）；两者都不存在时**不输出** `<link rel="icon">`。 |
| `logo` | 回退 `favicon` → 缺省图标 | **页头站标图标** URL，与站名并排显示（`<a class="header-brand">`）。留空依次回退：配置的 `favicon` → 缺省图标 → 只显示站名文字。 |
| `hero` | 空（不显示） | 首页 hero 背景图 URL；留空则整块 hero 区不渲染。 |

取值只允许：留空、站内绝对路径（`/x`）或 http(s) 绝对 URL；
协议相对形式（`//host/x`）会被启动校验拒绝。
URL 里**不允许**出现引号、圆括号、尖括号、反斜杠、空白与控制字符——
这些值会进入 HTML 属性甚至内联 CSS 的 `url()`，字符集必须先收窄
（否则 `&#39;` 经浏览器解码后会闭合 `url('…')`）。

### 样式表解析顺序

| `[static].css` | `use_builtin_css` | 结果 |
|---|---|---|
| 有值 | 任意 | 用配置的地址（站内路径或绝对 URL） |
| 空 / 缺失 | `true`（默认） | 用应用缺省样式表 `/css/style.css` |
| 空 / 缺失 | `false` | **不输出** `<link>`，页面无样式 |

`use_builtin_css` 只影响"配置为空时是否回落"，不会让显式配置失效。

### 通用静态服务（`elenvind/static/`）

应用把 `elenvind/static/` 整个目录作为自带的静态根发出，**URL 与磁盘一一对应**：

```
/css/style.css   ->  elenvind/static/css/style.css
/imgs/logo.svg   ->  elenvind/static/imgs/logo.svg
/fonts/x.woff2   ->  elenvind/static/fonts/x.woff2
```

也就是说：**往这个目录里丢文件就能被站点用上**，不需要改代码或登记白名单。

| 目录 | 约定用途 | 对应配置 | 缺省 URL |
|---|---|---|---|
| `elenvind/static/css/` | 样式表 | `[static].css` 留空时用 | `/css/style.css` |
| `elenvind/static/imgs/` | 图像 | `[static].favicon` 留空时用 | `/imgs/favicon.ico`（或 `.png`） |
| `elenvind/static/` 其它子目录 | 字体、图标等随意 | —— | 按相对路径直接取 |

响应带 `ETag` 与 `Cache-Control: public, max-age=86400`，**改动文件无需重启**
（按请求读盘，浏览器按 ETag 自动刷新）。Content-Type 按后缀给，
CSS/JS/SVG/字体等常见类型有显式映射（避免被回成 `text/plain` 而失效）。

替换缺省资产有两种方式，任选其一：

1. **直接改文件**（`elenvind/static/css/style.css`、`imgs/favicon.png`）——
   不用动配置，改完刷新即生效；
2. **改配置指到别处**——`[static].css` / `[static].favicon` 填 URL 后，
   配置优先，缺省文件不再被引用。

**安全边界**：规范化后必须仍在 `elenvind/static/` 之下；隐藏文件/目录
（任一段以 `.` 开头）一律 404；越界形态（`../`、`%2e%2e`、反斜杠、符号链接
指向外部）全部拒绝。这些资源是**公开**的（浏览器要取），
**站点私有内容不要放这个目录**。

> 其余资源（`logo`、`hero`、社交图标、以及你自己指定的 `css`）留给
> Nginx（或 CDN）托管也行——把 URL 填进 `[static]` / `params.social.icon`
> 即可；留空时对应元素**不渲染**，所以开箱即用不会裂图。
>
> 仓库根的 `style.example.css` 是缺省样式表的副本，可作为自定义样式的起点。

## `[params]` 首页与站点内容

| 键 | 说明 |
|---|---|
| `intro` | 首页 about 区介绍段落，原样转义输出；整行删除则该段落消失。 |

> 没有 `params.author`：文章作者来自每篇 `.md` 文件 front matter 里的
> `authors = [...]`（见 `docs/development/features.md` 第 6 节），
> 不设全局作者配置，避免出现"配置里写着作者但页面不读"的死配置。

### `[[params.nav]]` 顶栏/页脚导航（可多条）

| 键 | 说明 |
|---|---|
| `name` | 菜单显示文字（如 `Home`）。 |
| `url` | 跳转地址（站内 `/about` 或站外完整 URL 均可）。 |

导航末尾固定追加两个入口（无需配置）：主题切换（Dark/Light）与用户入口（Sign In / 昵称）。

### `[[params.social]]` 首页社交链接（可多条）

| 键 | 说明 |
|---|---|
| `name` | 平台名，作为图片 alt 与悬停提示。 |
| `url` | 链接地址（`http(s)://` 站外链接自动新窗口打开；站内路径不弹新窗）。 |
| `icon` | 图标图片 URL（建议托管在 Nginx/CDN）。留空则该条目退化为文字链接。 |

社交行自动按当前语言拼接成句：英文 `Find me on A, B and C.`、
中文 `在以下平台找到我：A、B 和 C。`、日文 `SNS で見つけてください：A、B と C。`。

### `[[params.projects]]` 首页 Projects 条目（可多条）

| 键 | 说明 |
|---|---|
| `name` | 项目名（加粗链接文字）。 |
| `url` | 项目链接；留空则只显示名称。 |
| `description` | 一句话描述；兼容 cactus 惯用的 `desc` 键名。 |

未配置任何条目时，首页对应区块自动整块隐藏；about 区（intro/社交）与 Projects
区块均按配置有无自适配，Writing 区块恒显示（无文章时给出 "No articles yet." 提示）。

## `[security]` 安全响应头

这些头由 **Core 在响应收尾阶段统一注入**，Feature 层完全不感知；
整节删掉也能正常启动（走代码内默认值）。它只覆盖**被动**安全头，
CSRF / 会话 / `trusted_proxies` 等主动安全不受本节影响。

> ⚠️ **TOML 陷阱**：表会"吃掉"它之后的所有顶层键。
> 因此 `[security]` 与 `[security.csp]` 必须写在 `config.toml` **最后**。
> 已有测试守着这条（`tests/test_security_headers.py`）。

| 键 | 默认 | 说明 |
|---|---|---|
| `csp_enabled` | `true` | 是否下发 `Content-Security-Policy`。`false` = 完全不下发（不推荐）。 |
| `permissions_policy` | 见下 | `Permissions-Policy` 头值。**留空字符串 = 不下发该头**；整键删掉 = 用应用默认值。 |
| `hsts_enabled` | 见下 | 是否下发 HSTS。真正的判据是"本项为真 **且** 请求是 HTTPS"。<br>未配置时跟随 `[server].cookie_prefix`（`__Host-` 前缀本身就要求全程 HTTPS）。 |
| `hsts_max_age` | `31536000` | HSTS 有效期（秒），取值 `0`–`63072000`（2 年）。首次上线建议先用 `300` 确认无误再调大。 |
| `hsts_include_subdomains` | `false` | 是否附加 `includeSubDomains`。子域**全部**支持 HTTPS 时再开，否则会把不支持 HTTPS 的子域一起锁死。 |

### HSTS 什么时候真的下发

```
hsts_enabled = true  ──┬──> HTTPS 请求 ──> 下发 max-age=…[; includeSubDomains]
                       └──> HTTP  请求 ──> 不下发（浏览器会忽略，误发还会锁死访客）
hsts_enabled = false ────────────────────> 一律不下发
```

`hsts_enabled` 缺失时等价于 `[server].cookie_prefix`。这是刻意的：
`__Host-` 前缀要求全程 HTTPS，开启它就说明站点已经把自己约束在 HTTPS 上。

### `[security.csp]` 指令表

只写想改的条目，其余保持默认。默认值：

| 指令 | 默认值 | 说明 |
|---|---|---|
| `default-src` | `'self'` | 兜底：同源放行，其余默认拒绝。 |
| `script-src` | `'none'` | 本站 Zero-JS，直接掐断脚本执行链（比放开 `'self'` 更安全）。 |
| `style-src` | `'self' 'unsafe-inline'` | **'unsafe-inline' 是必需的**：CSP 的 `style-src` 同时管 `<style>` 元素与 `style=` **属性**，而首页 hero 的后台图靠 `style="background-image:url(…)"` 设置。去掉它 hero 图会静默失效，控制台报 `style-src-elem`。注入风险由配置层兜住：`[static].hero` 要过字符白名单（禁引号/括号/反斜杠/空白）+ 协议白名单（只允许 http(s) 与站内路径）。<br>想彻底去掉 `'unsafe-inline'`，得先把 hero 改成非内联实现（写进 CSS 或用类名），再从配置里收紧本项。 |
| `img-src` | `'self' data: http: https:` | 正文图片与 `[static].hero` 可以是任意外部地址。 |
| `media-src` | `'self' http: https:` | `@video(url)` 支持外部视频。 |
| `font-src` | `'self'` | |
| `connect-src` | `'self'` | |
| `object-src` | `'none'` | |
| `base-uri` | `'none'` | |
| `form-action` | `'self'` | |
| `frame-ancestors` | `'none'` | |

三种写法：

```toml
[security.csp]
img-src = ["'self'", "data:"]              # 收紧：不再允许任意外部图片
object-src = false                         # 关掉：该指令整条不输出
style-src = "'self' https://cdn.example.com"   # 字符串形式也可以
media-src = "'self'"                       # 注意内层单引号是 CSP 语法的一部分；
                                           # 写成 "self" 会匹配一个叫 self 的主机，等于没放行
                                           # 空列表/空串才表示关闭该指令
```

**把样式表或图片放到 CDN 时，必须同时把该来源加进 `style-src` / `img-src`**，
否则页面会被自己的 CSP 拦掉（样式表被拦的表现是"完全没样式"）。

### 每个响应都会带的固定头

除上述可配置项外，以下头恒定注入，**没有关闭开关**
（关掉它们没有正当理由，且会削弱安全）：

| 头 | 值 |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Cross-Origin-Resource-Policy` | `same-origin` |

覆盖范围包括 404 / 405 / 500 错误页、静态资源、重定向，以及**请求对象还没构造出来时
的早期错误**（例如畸形 `Content-Length`）——因为它们都走同一个注入点。

### 给模板作者的两条约束

1. **不要写内联 `<script>`**。默认 `script-src 'none'` 会直接拦掉它，
   而且本站是 Zero-JS，没有理由引入脚本。
2. **不要新增内联样式**。默认 `style-src` 含 `'unsafe-inline'`，但目前
   **只有一个**合法的内联样式：首页 hero 的 `style="background-image:url('…')"`
   （值来自 `[static].hero`，经配置层字符与协议白名单校验）。
   新增 `<style>` 块或 `style=` 属性会让"为什么 CSP 放开了内联样式"变得说不清。
   若确有必要，请连同注释一起说明用途；若想彻底收紧 CSP，
   应先把 hero 改成非内联实现。

> 关于 CSP 报错文案：如果控制台出现
> `style-src-elem … Consider using a hash (requires 'unsafe-hashes' for style attributes) or a nonce`，
> 说明有内联样式被拦。**hash / nonce 只适用于 `<style>` 元素**；对 `style=`
> 属性必须配 `'unsafe-hashes'`，远不如直接放开 `'unsafe-inline'` 来得清晰可控。
> 本站默认已放开，正常情况下不应看到这类报错。

## 会话过期（`session_absolute_days` / `session_idle_days`）

会话是否有效要**同时**满足两个维度，任一超时即失效，并在读取时**当场从库里删除**
（不是只拒绝、留着以后再被利用）。

| 维度 | 键 | 默认 | 语义 |
|---|---|---|---|
| 绝对过期 | `session_absolute_days` | `30` | 从**创建**时刻算起，活够这么多天就必须重新登录。**与活跃程度无关**。 |
| 滑动过期 | `session_idle_days` | `15` | 从**最近一次活跃**算起，闲置超过这么多天即失效。每次有效访问刷新 `last_seen`。 |

```
创建 ──────────────────────────────────────────────► 绝对过期（30 天，硬上限）
      │←──── 15 天 ────→│
      每次有效访问把这里重置 ┘
```

为什么需要两个：只有滑动过期时，只要攻击者持续使用泄露的 token，它就**永不过期**；
只有绝对过期时，常用设备会在第 30 天被无预警踢出，体验差。两者配合才是
"常用设备留着、被遗忘的会话自然消失、泄露的 token 有硬上限"。

### 设为 0 = 该维度不过期

```toml
session_absolute_days = 0    # 只靠滑动过期
session_idle_days = 0        # 只靠绝对过期
session_absolute_days = 0
session_idle_days = 0        # ⚠️ 回到"会话永久有效"
```

> ⚠️ **两个都设 0 就等于移除了本机制**：token 一旦泄露，攻击者可以无限期使用它，
> 而你在服务端**看不出任何异常**（没有过期日志、没有失败记录）。
> 除非你完全清楚这个代价，否则不要这么配。
> 合法的最大值是 3650 天（10 年）——超过会被启动校验拒绝，
> 因为那基本等于"不想过期"，请显式写 0 让意图明确。

### 其他行为

- **启动时统一清理**：与 `comment_rate`、`login_attempts` 清理任务同一风格，
  启动日志会打印本次删掉了几条会话。
  运行期间读到过期会话也会顺手删除，启动清理只是兜住"长期没被访问"的那些行。
- **发证时也顺带清理**：登录颁发新会话时会扫一遍，避免长期不重启导致表膨胀。
- **Cookie 寿命跟随绝对过期窗口**（`session_absolute_days`）：
  它决定浏览器侧最多保留这个 Cookie 多久。滑动过期更短，没必要把 Cookie 留得更久。
  绝对过期设为 0 时 Cookie 回落到 7 天，而不是下发 `max-age=0`（那会导致立刻失效）。
- **时间戳被写坏时失败关闭**：`created_at` / `last_seen` 为 NULL 一律视为已过期。
  两列在 schema 层都是 `NOT NULL`，正常路径写不进 NULL。
- **改密 / 删号 / 登录**：仍会删除该账号的全部会话（与过期机制无关，语义不变）。

## `[logging]` 日志

| 键 | 默认 | 说明 |
|---|---|---|
| `level` | `"info"` | 日志级别：`debug` / `info` / `warning` / `error` / `critical`（其它值启动失败）。 |
| `file` | `"logs/app.log"` | 日志文件路径（**相对项目根**，目录不存在会自动创建）。 |
| `max_bytes` | `10485760` | 单文件轮转阈值（10 MB）。 |
| `backup_count` | `5` | 保留的历史日志份数。 |

## 启动校验（错误配置不会跑到页面才炸）

启动时会一次性校验：`locale` / `title` / `site_url` / `admin_user_id` /
`registration_enabled` / `max_length` / `max_comment_depth` /
`max_comments_per_article` / `session_absolute_days` / `session_idle_days` /
`max_body_size` / `database` / 内容目录 /
`[server]`（host、port、trusted_proxies、cookie_prefix）/ `[security]` /
`[static]` URL / `[pagination]` / 三个限流段落 / `[logging]`。
任一项非法都会打印原因并以非零码退出（lifespan 阶段则为
`lifespan.startup.failed`，uvicorn 不会启动一个半初始化的应用）。

## 常见修改示例

- 换首页介绍文字：改 `intro` 的值即可（支持 emoji）。
- 加导航项：在 `[[params.nav]]` 末尾追加一段相同结构。
- 加社交平台：追加 `[[params.social]]` 段，`icon` 建议指向自己托管的 SVG/PNG。
- 加项目展示：追加 `[[params.projects]]` 段。
- 全站切中/英/日界面：`locale = "zh"` / `"en"` / `"ja"`。
- 换域名：改 `site_url`（robots/sitemap 跟着变）。
- 关闭公开注册：`registration_enabled = false`。

## 注意事项

1. 语法校验：TOML 字符串用双引号包裹，含双引号需写成 `\"`；`[[...]]` 数组条目必须完整成段。
2. 改完**重启**才生效。
3. 出错不会静默：`config.toml` 解析失败或取值非法时服务拒绝启动（日志给出原因）。
4. 评论里提到的"图标绝对 URL"只是示例形态，域名换掉记得同步改 `[static]` 与全部 `icon`。
