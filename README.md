# Elenvind

一个只用 **Python 标准库 + Gunicorn + SQLite + Jinja2 + Markdown** 构建的个人博客/网站服务。

设计约束（项目的"宪法"）：

- **标准库为绝对核心**，第三方依赖只有 4 个：`gunicorn`、`Jinja2`、`Markdown`、`MarkupSafe`；
- 单体、单机、单库（SQLite），多 worker 通过 `flock` + `BEGIN IMMEDIATE` 串行化写事务；
- 不引入中间件框架 / ORM / 前端构建链：Zero-JS，服务端渲染；
- 分层单向依赖：`app（装配）→ modules（业务）→ db（持久化）`、`core（运行时基座）`，
  依赖方向由 `tests/test_architecture.py` 强制。

```
elenvind/
  app.py        装配点（唯一同时认识 core / db / modules 的地方）
  wsgi.py       WSGI 入口：gunicorn elenvind.wsgi:application
  core/         运行时基座：HTTP 边界、路由、会话、CSRF、安全头、Markdown、模板、i18n
  db/           持久化基座：唯一允许 import sqlite3 与执行 SQL 的地方
  modules/      业务模块：auth / users / admin / blog / pages / seo / system
  templates/    Jinja2 模板
  static/       内置静态资源（图标、样式表）
articles/       文章（TOML front matter + Markdown）
custom_pages/   自定义页面（纯 Markdown，文件名即路由）
i18n/           界面文案（en / zh / ja）
deploy/         Nginx / systemd / logrotate 示例
tests/          标准库 unittest 测试（含架构守卫）
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml     # 至少改 site_url 与 admin_user_id
.venv/bin/python run.py                # 默认 127.0.0.1:6789
```

首次启动会自动建库、建表、迁移，并在 `logs/app.log` 里记账。第一个注册的账号
是 `admin_user_id` 指向的用户（默认 1）——**先注册自己的账号**，或把
`admin_user_id` 改成你的 id、把 `registration_enabled` 先设为 `false`。

常用启动参数：

```bash
.venv/bin/python run.py --workers 2          # 覆盖 worker 数
.venv/bin/python run.py --bind 0.0.0.0:6789  # 临时对外（仅内网测试）
.venv/bin/python run.py --preload            # master 里导入应用（startup 只跑一次）
.venv/bin/python run.py --reload             # 开发：代码改动自动重载
```

## 生产部署（Nginx 反向代理）

**应用只监听 127.0.0.1**，由 Nginx 终结 TLS 并回源。理由与后果：

- 应用端口一旦可从公网直连，TLS/HSTS/边缘限速全部被绕过（启动日志会告警）；
- 反向代理还负责挡**慢连接**：Gunicorn 的 `sync` worker 只有 2 个，
  2 个"声明了 Content-Length 却不发正文"的连接就能让整站停摆到 worker 超时（30s）。
  Nginx 侧的 `client_body_timeout` / `client_header_timeout` / `limit_conn` 是必需的。

```bash
sudo cp deploy/elenvind.service /etc/systemd/system/
sudo cp deploy/logrotate.conf /etc/logrotate.d/elenvind
sudo systemctl daemon-reload && sudo systemctl enable --now elenvind
```

Nginx 配置见 `deploy/nginx.conf.example`（含 `proxy_hide_header Server`、限流与超时）。
配套的 `config.toml` 建议：

```toml
site_url = "https://your.domain"     # robots/sitemap 的绝对地址唯一来源
[server]
host = "127.0.0.1"
port = 6789
cookie_prefix = true                  # 全程 HTTPS 时启用 __Host- 前缀
[security]
hsts_enabled = true
```

## 备份与恢复

```bash
.venv/bin/python run.py --backup                    # → backups/elenvind-<时间戳>.db
.venv/bin/python run.py --backup-to /srv/backups/x.db
```

备份走 SQLite 的在线 backup API（WAL 模式下 `cp sqlite.db` 会丢掉还在 `-wal`
里的事务），并默认做 `PRAGMA quick_check`；失败会删掉半成品并返回非零退出码。
放进 cron 即可：

```cron
0 4 * * * cd /srv/elenvind && .venv/bin/python run.py --backup >> logs/backup.log 2>&1
```

恢复：停服 → 把当前 `sqlite.db` 挪走 → 用备份替换 → **删除 `sqlite.db-wal` 与
`sqlite.db-shm`** → 启动。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v     # 全部
.venv/bin/python -m unittest tests.test_auth_lockout -v    # 单个模块
```

测试只用标准库 `unittest`，**不会碰仓库里的 `config.toml` / `sqlite.db` / `logs/`**
（配置由夹具注入，数据库与日志落在临时目录）。覆盖：

| 文件 | 守什么 |
|---|---|
| `test_architecture.py` | 依赖方向、`sqlite3`/`jinja2`/`markdown` 只能在允许的层、写 SQL 只能在 db |
| `test_core_contract.py` | Markdown 净化、CSRF、回跳地址、静态资源穿越、请求 framing、安全头 |
| `test_comments.py` | 评论转义、深度硬约束、配额（涂黑即释放）、越权 |
| `test_auth_lockout.py` | **登录闸门不得阻断正确凭据**、会话轮换/节流、口令策略 |
| `test_logging_proxy.py` | 日志不含凭据/查询串、XFF/XFP 只在受信对端生效、Host 头不投毒 |
| `test_doc_consistency.py` | 配置校验（含 CSP 只许收窄）、i18n 三语一致、交付件与依赖声明 |
| `test_styles.py` | 模板/代码用到的 class 必须都在内置样式表里 |

静态检查（可选，仅开发工具，不是运行时依赖）：

```bash
.venv/bin/ruff check .        # 配置见 pyproject.toml
```

## 配置

全部配置项与语义见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。几个要点：

- 配置只在**启动时**加载一次并整体校验，非法配置直接拒绝启动（不会跑到某个页面才 500）；
- 路径类配置一律相对**项目根目录**解析，与启动时的 cwd 无关；
- 不需要任何签名密钥：会话是服务端随机 token，CSRF 是双提交 Cookie，密码是 scrypt 自描述哈希。

## 安全模型（一页速览）

- **会话**：32 字节随机 token 存服务端表，Cookie `HttpOnly` + `SameSite=Lax` + （HTTPS 时）`Secure`，
  可选 `__Host-` 前缀；登录轮换（防会话固定），改密/删号清空全部会话；绝对 30 天 + 滑动 15 天双过期。
- **CSRF**：双提交 Cookie，调度器对所有非安全方法**默认**校验，模块不写一行 CSRF 代码。
- **XSS**：Jinja 自动转义 + Markdown 白名单净化（裸 HTML 转义、协议白名单、危险标签连内容丢弃）。
- **注入**：所有 SQL 参数化；动态标识符只来自代码内白名单。
- **路径穿越**：静态资源经 `resolve()` 后必须仍在内置目录之下，且拒绝隐藏路径。
- **开放重定向**：回跳地址只接受站内路径（拒绝 `//`、反斜杠、TAB/控制字符）。
- **代理信任**：`X-Forwarded-For` / `X-Forwarded-Proto` 只在直连对端属于
  `[server].trusted_proxies` 时采信，且取最右非受信跳。
- **Host 头**：绝对地址只来自 `site_url` 配置，绝不从请求推导。
- **限流**：登录（邮箱 / IP / 全站三闸门 + 渐进冷却）、注册（IP）、评论（用户 / IP / 单篇配额）。
  **闸门只约束错误凭据**：正确密码永远不会被限流挡住（回归测试见 `test_auth_lockout.py`）。
- **日志**：访问日志不记查询串、请求体、Cookie 与凭据，非打印字符一律转义。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| 登录提示"稍后再试" | 触发登录闸门。**用正确密码仍可登录**；错误密码需等 `Retry-After` 秒。想立刻清空某邮箱的失败流水：`sqlite3 sqlite.db "DELETE FROM login_attempts WHERE email='x@y';"` |
| 表单提交返回 400 | CSRF 令牌与 Cookie 不匹配（页面开太久 / 清过 Cookie）。刷新页面重试即可，页面会给出提示 |
| 返回 413 | 请求体超过 `max_body_size`（默认 1 MB） |
| 返回 411 | 表单方法没带 `Content-Length`（多为自写客户端） |
| 文章/页面改了不生效 | 内容缓存最多 2 秒节流；也可重启或等待自动重扫（日志会记 "Article directory changed"） |
| 评论提示"已达上限" | 该文章未涂黑评论达到 `max_comments_per_article`；管理员涂黑旧评论即可释放名额 |
| 启动报"检测到上次迁移未完成" | 数据库里残留 `*_legacy` 表：先备份再按提示处理，绝不静默继续 |
| 非回环地址监听告警 | 你 bind 到了 `0.0.0.0` 且信任转发头：请改回 `127.0.0.1` 并让反代回源，或给端口加防火墙 |

## 许可

见 [LICENSE](LICENSE)。
