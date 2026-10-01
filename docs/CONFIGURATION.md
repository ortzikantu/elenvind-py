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
| `copyright` | — | 页脚版权署名，显示为 `© 2026 {copyright}`。 |
| `keywords` | — | SEO `<meta keywords>`；留空不输出。兼容纯字符串 `"k1, k2"` 或数组 `["k1", "k2"]`。 |
| `description` | — | SEO `<meta description>`；留空不输出。 |
| `admin_user_id` | `1` | **管理员账号 id**（可删/恢复任意评论、显示徽章）。缺失或非法时视为"无管理员"，不会静默把权限给 id=1。 |
| `admin_badge` | `"BIG BOSS"` | 管理员徽章文字；**留空 `""` 则彻底不显示徽章**。 |
| `deleted_user_nickname` | `"Journeyed On"` | 已注销账号的评论昵称占位文案。 |
| `max_length` | `1000` | 单条评论最大字符数（服务端校验与 textarea 双重限制）。 |
| `max_comment_depth` | `32` | 评论最大层级（顶层 = 1）。到顶后页面不再显示回复入口，后端同样拒绝。 |
| `max_comments_per_article` | `1000` | 单篇文章评论总数上限，超出返回 429。 |
| `registration_enabled` | `true` | 是否开放公开注册；`false` 时 `/register` 只显示"已关闭注册"。 |
| `max_body_size` | `1048576` | POST 请求体上限（字节，1 MB）。超出返回 413，缺 `Content-Length` 返回 411，非表单类型返回 415。 |
| `database` | `"sqlite.db"` | SQLite 文件路径（相对项目根）。**优先级：`ELENVIND_DB` 环境变量 > 本键 > 默认 `<项目根>/sqlite.db`**；本键留空时显式回落到默认路径（不会沿用进程里上一次的取值）。 |
| `articles_dir` | `"articles"` | 文章目录（相对项目根）。 |
| `custom_pages_dir` | `"custom_pages"` | 自定义页面目录（相对项目根）。 |
| `templates_dir` | `"elenvind/templates"` | Jinja2 模板目录（相对项目根）。整站换模板时才需要改。 |
| `use_builtin_css` | `true` | `[static].css` 为空时是否回落到应用缺省样式表。详见 [`[static]`](#static-静态资源地址)。 |

> 密码哈希参数（scrypt 的 N/r/p）**不**通过配置暴露：它属于安全默认值，
> 改了会让新老哈希不一致；升级算法请改 `elenvind/core/security.py` 并保留
> `password_needs_rehash` 的渐进式升级路径。

## `[server]` 服务监听

| 键 | 默认 | 说明 |
|---|---|---|
| `host` | `"0.0.0.0"` | 监听地址。**生产请改 `"127.0.0.1"`**，只让 Nginx 本机回源。 |
| `port` | `6789` | 监听端口（1–65535，越界启动失败）。 |
| `trusted_proxies` | `["127.0.0.1","::1"]` | **安全相关**：只有直连对端属于该列表时，`X-Forwarded-For` 才会被采信为客户端 IP（登录/评论限流按此计数）。`X-Real-IP` 一律不采信。切勿加入公网地址。 |
| `cookie_prefix` | `false` | 是否给会话/CSRF Cookie 加 `__Host-` 前缀。要求全程 HTTPS + `Path=/` + 无 Domain；仅在站点确定只能通过 HTTPS 访问时开启。开启后读取侧仍兼容旧的无前缀 Cookie，切换不会把所有人踢下线。 |

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
`max_comments_per_article` / `max_body_size` / `database` / 内容目录 /
`[server]`（host、port、trusted_proxies、cookie_prefix）/ `[static]` URL /
`[pagination]` / 三个限流段落 / `[logging]`。
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
