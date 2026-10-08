# 运维使用指南

> **适用读者**：日常维护站点的人（发文、管评论、看日志、处理限流误锁、备份）。
> **相关文档**：[部署](DEPLOYMENT.md) · [配置说明](CONFIGURATION.md) ·
> [安全模型](SECURITY.md) · [数据库与 C0](development/database.md) · [文档索引](README.md)

面向日常维护：内容管理、用户与评论、日志、数据库、限流与常见问题。
部署相关见《站点部署指南》，配置文件说明见《配置文件使用指南》。

## 目录

| 章节 | 内容 |
|---|---|
| [一、进程与日志](#一进程与日志) | 启停、systemd、日志文件与**日志都包含什么**、常用 grep |
| [二、内容管理](#二内容管理热更新无需重启) | 文章、自定义页面、静态资源（全部热更新） |
| [三、用户与评论](#三用户与评论) | 管理员、账号注销、会话 |
| [四、数据库](#四数据库) | 备份、巡检、解锁、清理 |
| [四之二、测试](#四之二测试) | 跑全量 / 单模块 / 冒烟 |
| [五、限流策略与误锁处理](#五限流策略与误锁处理) | 各维度阈值与解锁配方 |
| [六、主题与偏好](#六主题与偏好) | 明暗主题与偏好 Cookie |
| [七、常见问题（FAQ）](#七常见问题faq) | 症状 → 原因 → 处理 |
| [八、日常安全检查清单](#八日常安全检查清单) | 周期性核对项 |
| [九、上线部署核对清单](#九上线部署核对清单逐条确认) | 首次上线逐条确认 |

---

## 一、进程与日志

### 启停

```bash
# 前台（调试）
cd /opt/elenvind-py && .venv/bin/python run.py

# systemd（生产）
sudo systemctl start|stop|restart elenvind
sudo systemctl status elenvind
journalctl -u elenvind -n 100 --no-pager      # 看 systemd 侧输出
```

### 日志文件

- 位置：`logs/app.log`（`[logging].file` 可改），同时输出到控制台；
- 轮转：单文件 10 MB、保留 5 份（`max_bytes` / `backup_count` 可调）；
- 级别：默认 `info`；`debug` 会额外打印**每一笔写事务**与锁获取明细（见下表），
  排查完记得调回 `info`（debug 的日志量随流量线性增长）。第三方库
  （python-markdown / Jinja2）的 DEBUG 会被自动压到 `info`，所以 debug 日志里
  只有**应用自己**的细节，不会被扩展加载之类的噪音刷屏。

**日志都包含什么**（级别 → 内容）：

| 级别 | 内容 | 例子 |
|---|---|---|
| INFO | **每个请求一行访问日志**（Core 统一记） | `Request handled: GET /article/post -> 200 in 3.4 ms (5120 bytes, ip=203.0.113.5, user=3, pid=2481)` |
| INFO | 启动细节：日志文件、库路径、schema 版本、journal_mode、锁文件、耗时与 pid | `Database: /opt/elenvind-py/sqlite.db (schema v4, journal_mode=WAL, write lock /opt/elenvind-py/sqlite.db.write.lock)`、`Startup complete in 142 ms (pid=2481, 2 startup hook(s), locale=zh)` |
| INFO | 认证审计：登录成功/失败、注册成功/被拒、改密、删号、注销 | `Login succeeded: user_id=3 ip=203.0.113.5`、`Logout: user_id=3 ip=…` |
| INFO | 评论审计：发表、涂黑删除、编辑 | `Comment created/redacted/edited: id=… slug=post user_id=…` |
| DEBUG | 每笔写事务：耗时、改动行数、库路径 | `Write transaction committed: 2.1 ms, 1 row(s) changed (path=/opt/elenvind-py/sqlite.db)` |
| DEBUG | 锁获取明细（无竞争时） | `Write lock acquired in 0.31 ms (path=…)` |
| WARNING | 锁等待 ≥ 1s / 写事务 ≥ 1s | `Write lock wait 1101 ms (path=…)`、`Write transaction slow: 1523 ms, 1 row(s) changed (path=…)` |
| WARNING | 限流命中与安全信号：登录闸门（全局/邮箱/IP）、注册限流、评论限流、改密时当前密码错误、删号密码错误、未知表单动作、静态资源越界 | `Login blocked (email failure limit): ip=…` |
| ERROR | 文章/页面解析失败（点名文件）、未捕获异常（完整堆栈）、i18n 文件缺失 | — |

**刻意不记**（安全红线，有测试守着 `tests/test_logging_proxy.py` +
`tests/test_runtime_logging.py`）：查询串、请求体、Cookie、`Set-Cookie`、
密码与密码哈希、会话/CSRF 令牌、Referer、User-Agent、邮箱地址（PII）。
访问日志只记 `PATH_INFO`，且控制字符会被转义（防止客户端用 `%0a` 伪造日志行）。

相关命令：

```bash
tail -f logs/app.log                      # 实时看访问与审计
grep "Request handled" logs/app.log | tail -50
grep -E "WARNING|ERROR" logs/app.log | tail
grep "Write transaction slow" logs/app.log        # 慢写排查（C0 写竞争）
grep "Login blocked" logs/app.log                 # 爆破排查
grep "pid=2481" logs/app.log                      # 只跟某一个 worker 看
```

## 二、内容管理（热更新，无需重启）

### 文章

- 目录：`articles/`，每篇一个 `.md` 文件（文件名即 URL slug），
  正文是标准 Markdown，文档头字段见 `docs/development/modules.md`；
- **增、删、改即时生效**：索引按目录文件状态（mtime/size）自动重扫，
  正文按文件状态缓存，改完保存即可，无需重启；
- 首页按 front matter 的 `date` 倒序排列（缺 date 视为最早）；
- 单文件上限 1 MB，超出会拒绝解析并在日志报错；
- 图片/视频等媒体请放静态托管处或直接给绝对 URL：
  - 图片用标准 Markdown 语法 `![alt](https://…)`；
  - 视频用 **`@video(https://…)` 指令**（单独一行），它会渲染成
    `<video controls>`。**不要写原始 `<video …>` 标签** —— 正文里的原始 HTML
    会被转义成可见文本（这是 Zero-JS + 净化设计的一部分，不是 bug）。

### 自定义页面

- 目录：`custom_pages/`，文件名即路由（`about.md` → `/about`），同样热更新；
- 页面为纯 Markdown 正文（无 front matter），整页渲染为文章排版。

### 静态资源

`elenvind/static/` 整个目录由**应用自己**发出，URL 与磁盘一一对应
（`/css/style.css` ↔ `elenvind/static/css/style.css`，**没有 `/static` 前缀**）。
同步文件后刷新即可，不需要重启。

浏览器侧统一 `Cache-Control: public, max-age=86400`（**1 天**，所有静态类型一致），
同时带一个基于文件内容的 `ETag`：改了文件刷新即刻生效，不会让你一直看到旧副本。
若挂了 CDN 且想让改动更快扩散，清理 CDN 缓存或在 URL 上加查询串即可。

（如果让 Nginx 直接服务这些路径，就由 Nginx 的 `expires` 决定缓存时长，
以 `docs/nginx.conf.example` 为准。）

## 三、用户与评论

### 管理员

- `config.toml` 顶层 `admin_user_id` 指定的用户即站长（默认 `1`，即最早注册的账号）：
  导航带徽章、可**涂黑**任意评论（不可逆）；
- **要取消管理员**：把该键显式写为 `null`（`admin_user_id = null`）。
  写成 `0` / 负数 / 非整数会**拒绝启动**——这是刻意的，避免"以为关掉了管理入口、
  其实配置没生效"；
- 删除 = **永久涂黑**：正文在数据库里被等长黑块（U+2588，上限 400 块）替换。
  原文立即从库里消失，任何角色（含管理员）都无法恢复，**没有恢复入口**；
- 保留评论行（`is_deleted = 1`）而不是物理删除，是为了 `parent_id` 树不散架 ——
  被涂黑评论的子回复仍然挂在原来的位置上；
- 作者可以**编辑自己的评论**（复用评论区同一个输入框：`?edit=<id>#comment-form`）；
  管理员可以涂黑别人的话，但不能改写它；
- 若确需"彻底移除某行"（例如法律要求），那是库外运维动作：停服 → 直接 DELETE →
  自行处理其子树（产品内没有这个入口）。
  数据库层 `parent_id` 是 `ON DELETE SET NULL`，因此即便在库外手工清理了父行，
  子评论也会自动升级为顶层评论，不会跟着消失。

### 账号注销（用户自行操作）

- 逻辑删除：昵称清空、登录失效、`is_deleted=1`；其评论保留并显示
  `deleted_user_nickname`（默认 "Journeyed On"）占位；
- 被删账号的邮箱不可再注册（占位邮箱隔离 UNIQUE 约束）。

### 会话

- **绝对过期 30 天**：会话创建后活够 30 天就必须重新登录，与活跃程度无关
  （token 泄露后风险窗口的硬上限）；
- **滑动过期 15 天**：闲置超过 15 天即失效；有效访问会刷新 `last_seen`，
  所以常用设备不会被踢；
- 两者都由 `[server]` 之外的顶层键 `session_absolute_days` /
  `session_idle_days` 控制，设为 `0` 表示该维度不过期；
- 登录会先清除该账号**全部**旧会话再颁发新 token（防会话固定）；
- 修改密码 / 注销账号会**立即踢掉该账号全部会话**并清除浏览器 Cookie，
  表现为"被强制回到登录页"，属预期行为；
- 会话 Cookie 只在**会话发生变化**时下发（登录/轮换/登出），
  普通页面与静态资源请求不会回带 `Set-Cookie`。

## 四、数据库

- 文件：`config.toml` 顶层 `database`（默认项目根 `sqlite.db`；`ELENVIND_DB` 环境变量可覆盖），
  WAL 模式，每个连接都开启 `PRAGMA foreign_keys=ON`，启动自动建表/补索引/执行迁移；
- schema 版本记录在 `PRAGMA user_version`（当前版本见 `db_base.SCHEMA_VERSION`）。
  启动时若版本落后，会自动执行迁移（含重建表）并更新版本号，**幂等、可重复执行**，
  旧库无需手工处理；升级前仍建议备份；
- 迁移在**单个事务**内完成并且处于 C0 写协调边界内（跨进程 `flock` + 显式
  `BEGIN IMMEDIATE`），失败整体回滚、版本号不变；因此**多个 Gunicorn worker 同时
  启动也不会并发迁移**（后到的 worker 在锁上排队，进来时版本已是最新，直接跳过）；
  若发现上次中断留下的 `*_legacy` 备份表，**拒绝启动**并打印表名
  （而不是带着半迁移的数据继续跑，那会表现为"评论全部消失"）；
- 写协调（C0）要点，排障时最常需要知道的三件事：
  1. **锁文件**与数据库同目录：`sqlite.db` → `sqlite.db.write.lock`。它是稳定文件，
     **不要删除**（删掉重建会让不同进程锁住不同 inode，互斥失效）；
  2. 所有写事务都必须经过 Core 的 `write_tx()`：一次只有一个进程在写
     （`flock` 只保证排他互斥，**不保证先来先得**）；
  3. 锁等待超过 1 秒会在日志里出现 `Write lock wait …ms`。频繁出现说明写竞争明显，
     先查慢写事务或外部脚本，而不是去加 worker（**写是串行的**，加 worker 只对读有帮助）；
- 完整性自检（可放进日常巡检）：

```bash
sqlite3 sqlite.db "PRAGMA integrity_check;"     # 期望输出：ok
```

- 备份（WAL 下勿直接拷贝）：

```bash
sqlite3 sqlite.db ".backup 'backup-2026-01-01.db'"
```

- 恢复：停服 → 用备份文件替换 `sqlite.db`（同时删除残留的 `-wal`/`-shm`）→ 启动；
- 主要表：`user`、`session`、`login_attempts`、`register_attempts`、`comment`、`comment_rate`。
  不要手工改 `user.password`：密码哈希是自描述格式
  （`scrypt$ln=15,r=8,p=1$盐$摘要`；历史 `盐$摘要` 为 PBKDF2-SHA256 10 万次），
  填错会导致该账号无法登录。改算法/参数由代码负责，旧哈希会在用户下次成功登录时
  自动重新哈希（渐进式升级，不需要强制全员改密）。
- **什么时候该换数据库**：C0 只解决"同一台机器上多进程写 SQLite"的协调问题。
  如果出现下面任一情况，正确答案是迁移到 PostgreSQL，而不是继续给 SQLite 加锁：
  需要**多台机器**同时提供服务；写竞争导致的锁等待成为常态（日志里持续出现
  `Write lock wait`）；需要长时间运行的复杂写事务；需要远程/网络存储数据库文件。

## 四之二、测试

项目自带标准库 `unittest` 测试套件（无第三方测试依赖）：

```bash
cd /opt/elenvind-py
python -m unittest discover -s tests -t .          # 全部测试
python -m unittest tests.test_http -v              # 单个模块
```

测试使用临时数据库与临时内容目录（`.testtmp/`，已在 .gitignore 中），
**不会触碰生产数据库与文章目录**。升级代码后建议先跑一遍。

需要一次"真实 Gunicorn 冷启动"验证时（例如换机器、换 Python 版本后），
可以跑冒烟驱动（在**项目根目录**，不在 `scripts/` 下）：

```bash
python smoke_driver.py    # 需要环境里已安装 gunicorn
```

它会在临时目录（`.smoketmp/`）里起一个**真实的 Gunicorn（默认 2 个 worker）**
并走完整流程：首页 / 文章 / Markdown / 自定义页面 / 内置样式表 / 登录 / 注册 /
注销 / 改密 / 发表评论 / 回复评论 / 涂黑删除 / 编辑评论 / SEO / 主题 / 404 / 405，
以及请求体边界（411 / 413 / 415）与 Host 头投毒，共 61 项断言。

端口默认自动挑选空闲端口；要固定端口可设 `SMOKE_PORT`，要改 worker 数可设
`SMOKE_WORKERS`。多 worker 下所有状态都在 SQLite 里，因此"这个 worker 发的
评论、那个 worker 渲染出来"必须成立。

从任何工作目录运行都可以（脚本自己切到项目根），结束后清理临时目录，
不影响仓库里的真实数据。

## 五、限流策略与误锁处理

| 场景 | 维度与阈值 | 说明 |
|---|---|---|
| 登录 | 单邮箱 24h 内 5 次失败 | 主防线，防定向爆破；邮箱匹配不区分大小写 |
| 登录 | 单 IP 15 分钟 20 次失败 | 短窗口防脚本轮询，NAT 用户不易被长期误锁 |
| 登录 | 全站 15 分钟 200 次失败 | 分布式爆破最后闸门 |
| 评论 | 单用户 60 秒 5 条 / 单 IP 60 秒 10 条 | 命中即 429 并写 warning 日志 |
| 注册 | 单 IP 1 小时 5 次尝试 | 命中显示"注册尝试过于频繁"；可整体关闭注册 |

以上阈值都可在 `config.toml` 的 `[login_limits]` / `[comment_limits]` /
`[register_limits]` 段落调整（见《配置文件使用指南》）。

**限流现在是渐进式冷却**（不再是"锁 24 小时"）：达到阈值后等待时长 = `60s × 2^(超出次数)`，
封顶 24 小时，冷却结束即可重试；被限流时响应带 `Retry-After`，日志里能看到
`Login blocked (email failure limit): ip=… retry_after=…s`。
正常用户偶尔打错密码，等约一分钟即可 —— 只有持续爆破会迅速逼近封顶。

误锁/需要立即解锁（例如家庭 NAT 被他人拖累）：

```bash
sqlite3 sqlite.db "DELETE FROM login_attempts WHERE email = '你@邮箱.com';"   # 解单账号
sqlite3 sqlite.db "DELETE FROM login_attempts WHERE ip = '1.2.3.4';"          # 解单 IP
# 注：登录成功后该账号失败流水自动清除，一般无需手工干预
```

评论限流窗口仅 60 秒，等一分钟即可，无需处理。

## 六、主题与偏好

- 白/夜切换记录在 `theme` Cookie（1 年），无该 Cookie 时跟随系统 `prefers-color-scheme`；
- 手动清除方式：浏览器删除站点 Cookie，或访问 `/theme?mode=light|dark` 覆盖；
- 该 Cookie 与登录会话无关，登出不影响主题选择。

## 七、常见问题（FAQ）

| 现象 | 原因与处理 |
|---|---|
| `Address already in use` | 端口被占：`ss -ltnp | grep 6789`，或改 `[server].port` |
| 页面 500 | 先看 `logs/app.log` 尾部堆栈：文章语法错误会在日志点名文件；修复即热更新 |
| 控制台报 CSP 拦截 inline script | 页面本身零脚本；通常是**浏览器扩展**注入被 `script-src 'none'` 正确拦下，忽略即可 |
| 评论图标/社交图标裂图 | `params.social.icon` 或 `[static]` URL 与 Nginx 实际路径不一致；先 curl 该 URL |
| 页头没有站标图标 | `[static].logo` 与 `[static].favicon` 都为空 → 按设计只显示站名文字；填上任一个即可 |
| 登录后提示 "Too many failed attempts for this account…" | 单邮箱失败 5 次；等 24 小时窗口或按第五节 SQL 清除 |
| 改了 config.toml 没生效 | 配置非热加载，需重启 |
| 图片并排错位 | 用 `![alt](url){: width="50%" }` 属性列表控制宽度，多图之间不要留空行才会并排 |
| 日期显示不对 | 文章 front matter 的 `date` 请写带时区的 ISO 格式（如 `2026-01-05T10:00:00+08:00`），显示统一为本地 YYYY-MM-DD |

## 八、日常安全检查清单

1. 应用端口未被公网直连（`host=127.0.0.1` 或防火墙）；确认 `trusted_proxies` 只含真实代理；
   **这是本应用最重要的前置条件**：端口一旦公网可达，客户端可直接伪造
   `X-Forwarded-Proto: https`（受信代理下应用才会采信它），并绕过全部代理假定。
2. 全站 HTTPS 可达，登录 Cookie 带 `Secure`；
3. 若代理不在本机：把该地址写进 `trusted_proxies`；直接用 Gunicorn CLI 启动时
   还要同时传 `--forwarded-allow-ips="<代理地址>"`，否则 HTTPS 下 Cookie 不会带
   `Secure`（见《配置文件使用指南》代理信任边界）；
4. 定期 `git pull` 跟进安全修复，升级前备份数据库；
5. 偶尔翻阅 `login_attempts` 里的失败流水是否有异常来源 IP；

## 九、上线部署核对清单（逐条确认）

| # | 项目 | 期望 |
|---|---|---|
| 1 | HTTPS | 浏览器访问全站跳转 HTTPS，证书有效 |
| 2 | `site_url` | 已填成真实对外域名（robots/sitemap 的绝对 URL 只来自它） |
| 3 | `Secure` Cookie | 开发者工具里 `session`/`csrf` 带 `Secure`（经 HTTPS 访问时） |
| 4 | `__Host-` Cookie | 若 `cookie_prefix = true`：`Set-Cookie` 名为 `__Host-session`/`__Host-csrf`，且站点**只能**通过 HTTPS 访问 |
| 5 | `trusted_proxies` | 只包含真实代理地址（默认回环）；不含任何公网地址 |
| 6 | 代理转发头 | 代理不在本机时，把该地址写进 `[server].trusted_proxies`（Gunicorn CLI 启动时同时传 `--forwarded-allow-ips`） |
| 7 | 应用端口 | 防火墙/安全组封闭，公网无法直连 `host:port` |
| 8 | 数据库备份 | 已配置 `sqlite3 … ".backup …"` 定时任务（WAL 下勿直接拷贝文件） |
| 9 | 数据库权限 | `sqlite.db` 与锁文件 `sqlite.db.write.lock` 仅服务账号可读写，**目录**不可被他人写入（锁文件与数据库同目录，必须可创建文件） |
| 10 | 日志权限 | `logs/` 仅服务账号可读写；确认日志内不含密码/token（应用已保证） |
| 11 | 注册策略 | 需要私有站点时设 `registration_enabled = false`，或保持注册并依赖 IP 限流 |
| 12 | 管理员账号 | 第一个注册的账号 id 记为 `admin_user_id`，或显式设定；确认导航出现管理员徽章 |
| 13 | 静态资源 | `[static]` 与 `params.social.icon` 的 URL 在浏览器可 200 打开（无裂图） |
| 14 | 文件系统 | 数据库与锁文件都在**本地文件系统**，且同一套数据库只被一台机器使用 |
| 15 | 自检命令 | `python -m unittest discover -s tests -t .` 全绿；`python smoke_driver.py` 全绿；`sqlite3 sqlite.db "PRAGMA integrity_check;"` 输出 `ok` |
