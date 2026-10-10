# Elenvind 生产级审查报告

审查对象：`/home/hao/Dev/elenvind`（v0.3.1-dev，Python 3.14.7）
审查方式：全量源码走查（8137 行 Python + 525 行模板 + 配置 + i18n）+ 在 `/tmp` 副本上进行黑盒动态验证（Gunicorn 3 个实例、2 workers / 1 worker，共 8 组探针）。
说明：所有动态验证都在项目副本上做，原始目录未产生任何写入（`__pycache__`/`.ruff_cache` 已在审查后清理）。

---

## 0. 结论摘要

**这是一个"安全工程水平远高于可用性工程水平"的项目。**

- 应用层安全基本面（注入、XSS、CSRF、会话、开放重定向、路径穿越、代理信任、日志注入、错误泄漏）**没有发现可利用漏洞**：9 类 XSS 载荷、12 类路径穿越、跨用户评论操作、`/admin` 越权、伪造令牌全部被正确拦截。
- 但在**可用性 / 拒绝服务**这一面上存在 4 个可在数秒内验证的高危缺陷，其中 2 个正是你问的"登录拒绝攻击"：**定向账号锁定（5 次请求锁死任意已知邮箱，最长 24h 且无自救路径）**、**全站登录拒绝（200 次失败锁死所有人登录）**，另有**慢请求 2 个连接即可让全站停摆 30 秒**、**任何已注册用户可为每个静态资源请求独占全局写锁**。
- 工程完备度是最大短板：仓库里**没有 tests/、没有 README、没有 docs/、没有 systemd/nginx/备份/CI/依赖锁**，而代码里约 20 处注释把 `tests/test_*.py` 当作既存事实引用，另有 10 个函数只服务于"不存在的测试"，因此"声明式安全"这些最重要的不变式**目前没有任何自动化守卫**。

### 评分（生产级测评）

| 维度 | 分数 | 说明 |
|---|---|---|
| 架构与分层 | 9.5 / 10 | core / db / modules / 装配层 依赖单向，唯一 HTTP 边界、唯一写入口，无环 |
| 应用安全实现 | 8.5 / 10 | 基本面无洞；扣分在登录闸门设计、写放大、口令策略 |
| 并发与数据一致性 | 9.0 / 10 | flock + BEGIN IMMEDIATE + 幂等迁移 + 残留检测，教科书级 |
| 可用性 / 抗 DoS | **4.0 / 10** | 慢请求耗尽 worker、登录拒绝、写锁放大 |
| 数据库与运维 | 6.5 / 10 | 迁移优秀；评论不可删除导致配额永久耗尽、日志轮转多进程不安全、无备份 |
| 用户体验 | 7.0 / 10 | 错误页友好；锁定无出路、中文站混英文提示、无找回密码 |
| 自动化测试 / 验证 | **2.0 / 10** | 测试全缺，安全不变式无人守 |
| 部署 / 文档 / 交付 | **3.5 / 10** | 无 README/docs/部署件/CI/锁文件；`--bind` 覆盖时暴露告警失效 |
| 代码质量 / 可维护性 | 8.5 / 10 | 可读性极好；扣分在死代码、58 条 ruff 告警、注释与代码比过高 |

**综合生产级评分：6.8 / 10**

- 视作"个人博客 + 正确配置的 Nginx 前置"：**8.0 / 10（可以用，但要先修 P1）**
- 视作"当前仓库按 `config.toml` 直接 `0.0.0.0` 对外跑"：**4.5 / 10（不要这么做）**

一句话：**安全设计 9 分，抗攻击 4 分，交付完备度 3 分。**

---

## 1. 结构与架构评审

### 1.1 分层（优）

```
run.py / elenvind/wsgi.py        入口：配置校验 → Gunicorn
elenvind/app.py                  装配（Composition Root，唯一同时认识 core/db/modules 的地方）
elenvind/core/                   运行时基座（不认识 db、不认识 modules）
elenvind/db/                     持久化基座（不认识 core/modules，路径由装配层注入）
elenvind/modules/                业务（可依赖 core/db，模块之间零 import）
templates/ static/ i18n/         视图 / 内置资源 / 文案
```

判断：依赖方向是**真实成立**的（我逐个核对过 import），不是只写在注释里。

- `core` 不认识持久化：会话/用户通过 `store` callable 注入（`core/session.py`、`app.py:139-148`）。
- `db` 不认识配置：路径、会话窗口都由装配层以函数注入（`app.py:90-95`）。
- 模块之间零 import：SEO 需要的文章列表由装配层显式传参（`app.py:157`）。

这是整个项目最值钱的部分。它让"将来换 SQLite / 换模板 / 加模块"都是局部改动。

### 1.2 单一实现点（优）

多个**曾经重复过、现在收敛成一份**的关键实现，注释里都写明了"为什么必须只有一份"：

| 关注点 | 唯一实现 |
|---|---|
| HTTP 解析 / 请求体上限 / Cookie 下发 | `core/http.py` |
| 安全响应头 | `core/security.py:build_security_headers` |
| CSRF | `core/csrf.py` |
| 回跳地址校验 | `core/http.py:safe_next_path` |
| Cookie 解析 | `core/security.py:parse_cookie_header` |
| slug 校验 / 内容格式 | `core/content.py` |
| 评论层级计算 | `db/comment.py:comment_depth` |
| 评论树展开 | `db/comment.py:flatten_comment_tree` |
| Markdown 净化 | `core/markdown.py` |
| 写事务 | `db/transaction.py:write_tx` |
| 会话 Cookie 策略 | `core/security.py:_make_cookie` |
| 偏好白名单 | `core/security.py:PREFERENCE_COOKIES` |

### 1.3 架构上的真实缺口

1. **没有守卫测试**。`elenvind/app.py` 开头写着"依赖方向由 `tests/test_architecture.py` 守卫"，`core/templating.py` 说"由 `tests/test_core_contract.py` 静态守卫"，`config.toml` 说"守卫（tests/test_doc_consistency.py）只允许收窄 CSP"。仓库里**没有 tests/ 目录**。这些约束今天只靠人的自觉。
2. **`core` 里的 `build_render_context` 知道业务模板变量**（nav / social / projects / admin_badge / theme）。它把 `config.toml [params]` 的形状固化进了"基座"，属于 config→模板的隐式契约；改配置结构要动 core。
3. **`core/security.py` 同时装 Password / Cookie / CSRF 原语 / CSP 定义 / 权限判定**（533 行）。职责偏多，但都是"安全原语"，尚可接受。
4. **模板与 Python 有一处重复判断**：`base.html:4` 直接读 `request.cookies.get('theme')`，而 `core/context.py:92` 走 `request.preference("theme")`（白名单在 core）。两处当前结论一致（因为白名单内值相同），但这是策略漂移的种子——`base.html` 绕过了 core 的白名单。
5. **`admin_badge` 语义分散**在两处（`context.py:102`、`modules/blog/logic.py:312`），默认值 "BIG BOSS" 各写一遍。

---

## 2. 漏洞与攻击面（按严重度）

> 证据栏中的数字都是在副本上实测得到的。

### P1-A（高危｜可用性）定向账号锁定：5 次请求锁死任意已知邮箱

**位置**：`modules/auth/routes.py:191-238` + `db/auth.py:84-137`（`reserve_login_attempt`）

**机制**：
1. 登录闸门在**校验密码之前**执行，且按"提交的邮箱"计数：`email_window_seconds` 默认 **86400**、`max_email_failures` 默认 **5**。
2. 达到阈值后 `allowed=False`，请求**直接返回锁定提示，根本不进入密码校验**——所以受害者用**正确密码**也进不去。
3. 失败流水**只有登录成功才会被清空**（`complete_login_success` → `DELETE FROM login_attempts WHERE email=?`）。而锁定状态下不可能登录成功 → **自锁死闭环**。
4. 窗口是 24 小时，冷却还是翻倍式 backoff，攻击者每天补 5 次即可**无限期**维持锁定。

**实测**：
```
attacker wrong-password #1..#5 : 200 邮箱或密码错误。
attacker wrong-password #6     : 200 Retry-After=59 该账号失败次数过多…
VICTIM with CORRECT password   : 200 该账号失败次数过多…（302 出现即代表成功，此处没有）
```

**影响**：任何知道目标邮箱的人（个人博客的站长邮箱通常是公开的）可以用 5 个 HTTP 请求把站长/任意用户踢下线 24 小时以上。而项目**没有找回密码、没有邮件、没有管理员解锁入口、管理员页面也只有只读状态**（`modules/admin/routes.py`）——被锁的人只能去改数据库。这既是 DoS，也是"站长自己被锁 ⇒ 无人能解锁"的运营事故。

**最小修复（KISS，不动架构）**：把闸门从"先拒绝"改成"验证优先"——邮箱/IP/全局闸门只允许一次尝试通过（占位），随后**照常做 `verify_credentials`**：
- 密码正确 → 放行 + 清计数（受害者永不掉线）；
- 密码错误 → 才返回锁定提示。
这样爆破防护一点不减（错误密码仍被拒），但"锁定"不再能阻止合法用户，问题从根上消失。实现上只需把 `_try_login` 里的 `if not allowed: return …` 改成"记录 `allowed=False` 的降级状态，先校验密码，失败时再按 `reason/retry_after` 返回"。**另外建议**：把 `email_window_seconds` 从 86400 降到 900~3600（配合 backoff 已足够），并提供一个显式解封入口（见 §5）。

---

### P1-B（高危｜可用性）全站登录拒绝：200 次失败让所有人无法登录

**位置**：同 `db/auth.py:106-137` 的 `global` 闸门（`max_global_failures=200 / 900s`）

**实测**（peer=127.0.0.1 是受信代理，因此用 XFF 模拟多来源 IP）：
```
200 次跨"200 个 IP"的失败 → login_attempts 中 success=0 计数 = 200
随后一个完全无关的用户，用【正确】密码登录：
    status=200  msg='尝试过于频繁，请稍后再试。（约 1 分钟后可重试。）'
```
即：**全站任何人（包括管理员）都无法登录**。而且因为阻断期间不写流水，计数会停在阈值上，攻击者每分钟补一次就能长期维持。反过来，正常用户的输错（或共享出口 NAT 下多人输错）也会计入这个全局计数，属于**可自伤的闸门**。

**约束说明（不要夸大）**：远程攻击者需要通过 Nginx 才成立，且应用取 XFF 最右侧非受信跳（`core/utils.py:29-60`，逻辑正确），所以**单个真实 IP 无法伪造 200 个来源**——需要约 10 个以上来源 IP（每个低于 20 次的 IP 阈值）。有 10 个 IP 的任何人（或任何能落到受信代理上的 SSRF/本地进程）都能做到。对一个个人博客，这个杠杆比过大。

**最小修复**：与 P1-A 同一条——**全局闸门只降级、不阻断合法凭据**；或把 `global` 从"硬拒绝"改成"强制延迟 + 告警"；或把全局计数改为"每 IP 聚合的独立性指标"（例如来源 IP 的基数），而不是简单总数。最省事的做法是把它变成 `logger.warning` + 每请求 sleep(1)（有限流效果、不可能锁死全站）。

---

### P1-C（高危｜可用性）慢请求耗尽 worker：2 个空闲连接让全站停摆 30 秒

**位置**：`run.py:168-179`（`worker_class = "sync"`, `workers = 2`, 未设 `timeout`）

**实测**：
```
baseline GET /                              : 0.003s 200
打开 2 个连接，声明 Content-Length: 100 后不发送正文
GET / 同时进行                              : 30.4s 200   ← 全站不可用
换成 2 个"请求头不写完整"的连接（GET / HTTP/1.1\r\nHost: … 不结束）
GET / 同时进行                              : 31.0s 200
```
Gunicorn 默认 30s worker 超时到点杀 worker，然后**重启 worker**——而每个新 worker 都会重跑 `startup()`（`init_db` 迁移 + 4 次清理写事务 + 文章/页面全量扫描）。攻击者可以循环触发，把"停机 + 反复重启 + 反复写库"叠加起来。

**影响**：`config.toml` 与 `config.example.toml` 出厂都是 `host = "0.0.0.0"`。按当前出厂配置直接对外，2 个 socket 就能让站点离线。**必须靠 Nginx**（见 §5 的指令），应用自身无法解决。

---

### P1-D（中高危｜可用性 + 性能）带会话的每个请求都会独占全局写锁（含静态资源）

**位置**：`core/app.py:158`（每请求 `user_loader`）→ `core/session.py:53-68` → `db/session.py:118-145`（`get_session_user` 无条件 `UPDATE session SET last_seen`，且整段在 `write_tx()` 里）

**实测**：
```
GET /css/style.css?w=0..19（带会话 Cookie）：20 请求 33.0ms
session.last_seen 前 -> 后：1791541738.900 -> 1791541738.933（确实被改写）
并发 8：匿名静态 2076 rps  vs  带会话静态 1145 rps
```
也就是说：**浏览器每加载一个静态资源，就取一次跨进程 flock + BEGIN IMMEDIATE + WAL 提交**。而 `core/http.py:229-244` 的注释声称"省掉了每取一张图片就写一次 last_seen 的无谓 DB 写"——**该注释与实际行为不符**（它省掉的只是 Set-Cookie，不是这次 UPDATE）。

**放大路径**：注册是开放的（默认 `registration_enabled = true`），任何拿到会话的人只要请求 `/css/style.css?cb=<随机>`（查询串不参与路由，缓存键却会变，ETag/条件请求拦不住），就能用每个请求独占全局写锁。由于**所有**写（评论、登录、会话创建、限流记账）都排在这一把锁后面，一个会话就能把写路径饿死。这在你问的"受攻击面影响用户体验"里属于典型的**低门槛资源独占**。

**最小修复**：给滑动过期加节流——`get_session_user()` 里只在 `now - last_seen > 300`（5 分钟）时才 UPDATE。15 天的滑动窗口完全不需要秒级精度，写入量会降 2~3 个数量级，且不动任何语义。更彻底一点：只有 HTM L 路由（或 `Accept: text/html`）才刷新 `last_seen`。

---

### P2-A（中危｜内容可用性）评论配额可被永久耗尽，且删除无法回收

**位置**：`db/comment_rate.py:86-91`（`SELECT COUNT(*) FROM comment WHERE article_slug = ?`，**不排除已涂黑**）+ `db/comment.py:67-87`（删除=涂黑，**保留行**）

**实测**（副本把上限改成 50 便于验证）：
```
填到上限：429 This article has reached the comment limit.
作为作者涂黑 10 条 → 全部 302
行数：50（不变）| redacted: 10
再发一条 → 429 This article has reached the comment limit.  ← 依然封顶
```
全仓库**没有任何** `DELETE FROM comment`（我 grep 过：只有 session / *_attempts / comment_rate 有 DELETE）。因此：

- 文章一旦达到 `max_comments_per_article`（默认 1000），**永久**无法再接受评论，连管理员涂黑都救不回来，只能手工改库。
- 攻击成本：`max_per_ip = 10/60s`，单 IP 约 100 分钟；多个 IP 十几分钟即可打满一篇热门文章。开放注册让门槛接近于零。

**最小修复**：计数改为 `WHERE article_slug = ? AND is_deleted = 0`（涂黑即释放配额，语义也正确）；若担心刷量，另加"每用户/每 IP 的累计发文上限"。

---

### P2-B（中危｜可用性放大）文章目录每请求全量 stat

**位置**：`modules/blog/logic.py:157-166`（`_has_changes()` 每次 `get_articles()` 都 glob + stat 整个目录）、`modules/pages/logic.py:75-85` 同理

**实测**：
```
   1 个文件: GET / =  1.01 ms
1001 个文件: GET / =  8.02 ms
5001 个文件: GET / = 35.49 ms     ← 线性，且完全匿名可达
```
对一个"文章多、评论/分享带来突发流量"的博客，这是**未认证即可触发的 CPU/IO 放大**（每次 GET /、每次文章页、每次 sitemap、以及每次 404 兜底都会触发）。当前文章数很少，所以还没疼。

**最小修复**：加一个 TTL 缓存（例如 2 秒内不重复 stat），或用 inotify 之外最朴素的方案——把扫描间隔从"每请求"改成"每 N 秒最多一次"（`_LAST_SCAN` 时间戳，10 行代码）。

---

### P2-C（中危｜运维）多 worker 日志轮转不安全 + 无备份方案

- `core/logging_config.py:76` 每个 worker 各持一个 `RotatingFileHandler` 指向同一文件。2 个进程同时 rotate 会丢行/错乱（项目注释里也承认了）。生产建议：改用 `logging.handlers.WatchedFileHandler` + 外部 `logrotate`（`copytruncate` 或 `create`），或让 systemd /journald 接管。
- WAL 模式下备份必须包含 `-wal`/`-shm` 或使用在线备份。仓库里**没有任何备份脚本/文档**，而库里装着用户、会话、评论。建议在 README 里给出 `sqlite3 sqlite.db ".backup '/path/backup-$(date +%F).db'"` + cron，或做一个 `run.py --backup`（stdlib 就能实现）。

---

### P3（低危 / 卫生项，均已实测或定位）

| # | 问题 | 位置 / 证据 |
|---|---|---|
| 1 | `--bind` 覆盖配置时暴露告警失效（**假阴性**，实测 0 条告警） | `app.py:114-133` 读的是 `config[server].host`，不是实际 bind；反向还会**假阳性**（我第一次用 `--bind 127.0.0.1` 启动，却被告知"正在监听 0.0.0.0"） |
| 2 | `RETENTION_DAYS` 被覆盖：模块顶部 `30`，第 209 行又赋值 `7`。运行时 `auth.RETENTION_DAYS == 7`，于是运行期 prune 登录流水按 7 天、启动清理按 30 天 | `db/auth.py:25,43,136,158,202,209`（实测 `module RETENTION_DAYS = 7`，`cleanup_old_login_attempts` 默认 30） |
| 3 | 死代码 10 处：`db/auth.py` 的 `record_login_attempt` / `count_email_failures` / `count_ip_failures` / `count_global_recent_failures`、`db/comment.py:create_comment`、`core/security.py:clear_session_cookie` / `theme_cookie_header` / `stylesheet_is_same_origin`、`core/templating.py:reset_environment`、`core/http.py:Response.set_cookie` / `delete_cookie`（后两个只在注释里被提到） | 全仓 grep 无调用点 |
| 4 | 行内代码被**二次转义**：`` `<script>` `` 渲染成 `&amp;lt;script&amp;gt;`，读者看到的是 `&lt;script&gt;` 字面量；围栏代码块正常 | `core/markdown.py:286-298` 的 `escape_raw_html_outside_code()` 只识别围栏/缩进代码，不识别行内 code span。实测 HTML：`<code>&amp;lt;script&gt;x…` |
| 5 | 中文站点混英文提示：评论/限流文案是硬编码英文（`modules/blog/logic.py:446-454`），错误页与 admin 页也是英文 | 实测：`Content cannot be empty`、`This article has reached the comment limit.`、`Reply depth limit reached` 出现在中文页面 |
| 6 | `/login`、`/register` 的 GET 响应是 `Cache-Control: no-cache`（含 CSRF 令牌的页面不 `no-store`）。共享缓存 304 复用会把 A 的令牌给 B（双提交模型下无安全影响，但 B 的表单会 400） | `core/http.py:593-605` |
| 7 | 口令策略仅长度 8~128，无强度/黑名单校验（实测 `12345678`、`aaaaaaaa` 均注册成功）；无找回密码、无邮箱验证、无 2FA | `core/security.py:73-76`、`modules/auth/routes.py:257` |
| 8 | 非法方法走早期错误路径，**405 不带 Allow**；`OPTIONS`/`TRACE` → 405（无 Allow）；`DELETE /`（无 Content-Length）→ 411 而不是 405 | 实测：`OPTIONS / -> 405 allow=None`；`DELETE / (CL=0) -> 405 allow=GET, HEAD` |
| 9 | `[params]` 的 `nav/social/projects` 的 url 与 `social.icon` **不经过** `_check_asset_url()`（只有 `[static]` 走）。配置由站长掌控，风险低，但"URL 校验单一来源"的宣称在此处不成立 | `core/config.py:255-259` vs `core/context.py:128-173` |
| 10 | `ruff check` 58 条（15 未用 import、12 import 未排序、5 `datetime.now()` 无 tz、5 `Optional` 隐式、等）；仓库无 `pyproject.toml`/ruff 配置/CI | `ruff 0.16.10` 实测 |
| 11 | `style.example.css` 与 `elenvind/static/css/style.css` **逐字节相同**（22781 字节），是冗余的第二份真相 | `diff -q` → IDENTICAL |
| 12 | 版本号 `v0.3.1-dev` 却按生产运行；`admin_user_id` 指向不存在/已注销用户时**无任何启动告警**（静默变成"无管理员"） | `core/version.py:1-7`；`core/security.py:242-248` |
| 13 | 文章/页面正文每次请求都要重新 stat（含缓存命中路径）、静态资源每次请求整份读入内存并计算 sha256 ETag（22KB CSS 每请求都要算一次） | `modules/blog/logic.py:178-207`、`core/assets.py:173-206` |
| 14 | 不隐藏 `Server: gunicorn`；无 `X-Permitted-Cross-Domain-Policies`；无 CSP `report-uri`（无 JS 也就没有报告通道，可接受） | 响应头实测 |

### 已验证"没有洞"的部分（重要，避免你重复投入）

| 检查项 | 结果 |
|---|---|
| SQL 注入（参数化 / 动态标识符） | 全部参数化；动态标识符只有 `db/maintenance.py:41-54` 白名单 + `_COUNT_SCOPES` 白名单 |
| 存储型/反射型 XSS（文章正文） | 9 类载荷（script/onerror/javascript:/data:/iframe/svg/form/style/协议相对）→ 解析后**零**可执行构造 |
| 评论 XSS | `<script>`、`onerror`、`"`、`'`、`&`、`\` 全部转义；`Markup` 只用在已转义结果上 |
| 昵称 XSS（属性逃逸） | HTMLParser 解析出的 `value` 属性与原始昵称完全一致，`"` 已转义，无法逃出属性 |
| CSRF | 无令牌 400 / 借用他人令牌 400 / 所有非安全方法默认校验；`GET /logout` 不产生副作用 |
| 开放重定向 | `//evil.test`、`/\evil.test`、`/%09/evil.test`、`javascript:` 全部回退 `/` |
| 路径穿越 / 隐藏文件 | 12 种形态（`..`、`%2e%2e`、`..%2f`、`//etc/passwd`、`.git`、`.env`）全部 404 |
| 会话固定 | 登录轮换、旧 token 立即失效、同账号仅 1 行会话 |
| 越权（评论删除/编辑） | 非作者 403 且 DB `is_deleted` 不变；作者本人成功；`/admin` 匿名跳登录、普通用户 403 |
| 评论深度 | `max_comment_depth=3` 时第 4 层被事务内权威判定拒绝（400），模板隐藏按钮也拦不住手工构造 |
| 请求体 framing | 缺 CL→411、非 UTF-8→400、JSON→415、>1MB→413、畸形/超长 Cookie→200 不炸 |
| Host 头投毒 | 站点绝对地址只来自 `site_url`（实测 `Host: evil.test` 不回显、sitemap 仍用配置域名） |
| 代理信任 | XFF 取最右非受信跳、XFP 与 XFF 共用同一份白名单；`X-Forwarded-Proto: https` 才出 HSTS 与 `Secure` |
| 错误处理泄漏 | 无 traceback / 无磁盘路径；错误响应也带完整安全头且 `no-store` |
| 日志注入 | `_log_field()` 转义控制字符并截断；访问日志不记查询串/正文/Cookie |
| 权限模型 | 只有 public/authenticated/admin/owner 四档，未知档位注册期即报错（fail-closed） |
| 迁移正确性 | flock + BEGIN IMMEDIATE 内的 DDL、残留 `*_legacy` 检测并拒绝启动、幂等 |
| 口令存储 | scrypt(N=2^15) 自描述哈希 + 渐进 rehash + `compare_digest` + 哑哈希抹平计时 |

### 关于第 5 点（管理员 id）

确认符合你的要求，且实现是干净的：`core/security.py:242-271` 只从 `config.admin_user_id` 取（负值/布尔/缺失 → 视为"无管理员"，不静默回退 1）；业务代码里没有 `id == 1` 魔法数；`admin_user_id = null` 可显式关闭管理员。**唯一提醒（一句话）**：默认值 1 在**全新部署 + 注册开放**时等于"第一个注册者即管理员"（我在副本上验证过：第一个注册账号访问 `/admin` 得 200 + 徽章）。上线前先自己注册，或显式写死 id，或先关注册。

---

## 3. 用户体验 / 拒绝服务（你关心的第 4 点）

**已被正确处理的 UX 安全点（值得肯定）**：

- 登录限流返回 `Retry-After` 与"约 N 分钟后重试"的具体时间（而不是含糊的"稍后再试"）；
- CSRF 失败给的是带布局的 400 友好页（说明"页面开太久/清过 Cookie，返回上一页刷新重试"），而不是一行英文；
- 未登录访问受保护 GET → 跳登录页并带 `next`（友好）；已登录但权限不足 → 403（不误导）；
- 销号前明确披露"删掉什么/保留什么"，并说明评论会显示成什么名字；
- `GET /logout` 只确认不生效，POST + CSRF 才真退出；
- 昵称一年只能改一次、改邮箱需当前密码、改密后全端强制重登；
- 静态资源有 ETag + 304，匿名 HTML 不 `public` 缓存（登录态 `no-store`）。

**会伤害用户体验/可用性的问题（按优先修）**：

1. **锁定即失联（P1-A/P1-B）**：错误密码 5 次 → 正确密码也进不去；无找回密码；管理员页无可操作项；无 CLI/文档化的解封手段。对"个人站"来说这等于**把自己锁在门外**。→ 必须按 §5-1 修复。
2. **慢连接即全站不可用（P1-C）**：30 秒白屏/超时，反复触发会反复重启 worker。
3. **每张图片/样式都想拿写锁（P1-D）**：正常用户多了会互相拖慢；一个坏用户能饿死写路径。→ 会话刷新节流。
4. **文章评论被刷满后永久关闭（P2-A）**：读者看到"已达上限"，站长也无法恢复。
5. **中文站里混英文提示（P3-5）**：评论区的错误提示尤其显眼（就在正文上方）。
6. **无找回密码 / 无邮箱验证（P3-7）**：忘记密码 = 账号作废；也意味着注册无需邮箱所有权（刷号成本低）。
7. **登录页/注册页未 `no-store`（P3-6）**：CDN/共享缓存下会出现"表单莫名其妙 400"的偶发体验问题。
8. **参数错误返回 405/411 的语义瑕疵（P3-8）**：自动化客户端/爬虫拿到 411 会困惑。

---

## 4. 目前"看起来在生产，其实缺失"的东西（交付清单）

| 缺失项 | 影响 |
|---|---|
| `tests/`（被引用 ~20 次） | 架构与安全不变式无人守，任何重构都可能静默破防 |
| `README.md` | 没有安装/首次运行/升级/备份/排障入口 |
| `docs/CONFIGURATION.md`（`config.example.toml:4` 明确引用） | 配置项语义只能读源码 |
| Nginx 示例配置 | 关键（TLS、超时、限流、`X-Forwarded-*`、`server_tokens off`） |
| systemd unit / 进程守护 | 无法开机自启、崩溃自愈 |
| 依赖锁文件 / 版本下限（如 Python ≥ 3.11，因为 `tomllib`） | 复现性、`pip` 装出不同版本 |
| CI / 静态检查（ruff） | 58 条告警无人处理 |
| 备份脚本 + 恢复演练 | 数据只有一份 `sqlite.db` |
| 健康检查端点 | 反代/监控无法探活（可用 `/robots.txt` 顶替） |
| 数据保留与隐私说明（`login_attempts` 存明文邮箱+IP） | 合规与用户信任 |
| 版本策略（`-dev`） | 发布可追溯性 |

---

## 5. 修复路线图（按性价比排序，全部保持 KISS / 最小改动）

**第 1 批（必须先做，1~2 小时内可完成，改动都在 2~3 个文件内）**

1. **登录闸门"验证优先"**（`modules/auth/routes.py:_try_login`）：闸门命中时不再直接返回，而是继续 `verify_credentials`；密码正确 → 放行并清计数（顺带解封），密码错误 → 返回原锁定提示 + `Retry-After`。**同时解决 P1-A 与 P1-B**，不引入新状态、不损失爆破防护。
2. **会话刷新节流**（`db/session.py:get_session_user`）：`if now - last_seen < 300: return row["user_id"]`（不写库）。**解决 P1-D**（写入量降 2~3 个数量级，静态资源不再独占写锁）。
3. **Nginx 必须项**（部署侧）：
   ```nginx
   server {
     listen 443 ssl http2;
     server_tokens off;
     client_header_timeout 10s;
     client_body_timeout   10s;
     send_timeout          10s;
     location / {
       proxy_pass http://127.0.0.1:6789;
       proxy_http_version 1.1;
       proxy_set_header Host              $host;
       proxy_set_header X-Real-IP         $remote_addr;
       proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_read_timeout 30s;
       limit_conn addr 20;                 # 挡慢连接（P1-C 的正面解法）
     }
     location = /login    { limit_req zone=login burst=5 nodelay; proxy_pass http://127.0.0.1:6789; }
     location = /register { limit_req zone=login burst=3 nodelay; proxy_pass http://127.0.0.1:6789; }
   }
   ```
   并把 `config.toml` 的 `[server].host` 改回 `127.0.0.1`、打开 `cookie_prefix = true`（全站 HTTPS 后）、防火墙只放 443/80。
4. **把出厂 `host = "0.0.0.0"` 改成 `127.0.0.1`**（`config.toml` + `config.example.toml`）。开发需要时用 `--bind 0.0.0.0:6789` 显式覆盖——顺手修掉 P3-1（让 `warn_on_exposed_bind` 接收**实际 bind**，别读配置）。

**第 2 批（一周内）**

5. 评论配额改为只计 `is_deleted = 0`（`db/comment_rate.py:86`），解决 P2-A。
6. 文章/页面目录扫描加 2 秒 TTL（`modules/blog/logic.py:157`、`modules/pages/logic.py:75`），解决 P2-B。
7. 加**最小测试集**（`unittest`，零新依赖）：① 架构守卫（core 不 import db、db 不 import sqlite3 以外…可照抄 `app.py` 注释里的规则）；② 路由安全声明（每个非 GET 路由必须被调度器保护）；③ 回归用例：锁定后正确密码仍可登录、redaction 释放配额、`safe_next_path` 拒 TAB/反斜杠/`//`、`resolve_asset` 拒穿越、`render_markdown` 拒 js/data。这 5 组用例能把本次发现的 4 个高危全部钉死。
8. 日志改 `WatchedFileHandler` + logrotate；补 `sqlite3 .backup` 备份脚本与恢复说明。
9. 补齐 `README.md`（含部署、备份、排障）与 `docs/CONFIGURATION.md`，或删掉对它的引用。

**第 3 批（顺手清理）**

10. 删死代码（§P3-3）、修 `RETENTION_DAYS` 覆盖（统一成一个常量）、补 `pyproject.toml` + ruff 配置并清零告警。
11. 行内代码转义修复（`escape_raw_html_outside_code` 增加行内 code span 状态机），或明确记录为已知限制。
12. 评论/错误页/admin 页接入 i18n；`admin/index.html` 与 `system/routes.py` 的英文串进 `i18n/*.toml`。
13. `/login`、`/register` 改 `no-store`；`base.html:4` 改用 `theme` 变量（与 `request.preference` 统一）。
14. 启动时校验 `admin_user_id` 是否存在，不存在则 warning；`version.stage` 按环境区分。
15. 删除重复的 `style.example.css`（或改为软链接/说明），避免两份真相。
16. 加一个显式的解封入口（`python run.py --unlock-email <email>` 或文档化的 SQL），保证任何锁定都可人工解除。

---

## 6. 附录：动态验证方法（可复现）

环境：项目副本 + `.venv/bin/python`（3.14.7），`run.py --bind 127.0.0.1:<port> --workers 2`，独立 SQLite；探针全部只用标准库（`http.client` / `socket` / `sqlite3`）。

| 探针 | 覆盖 |
|---|---|
| probe 1 | 安全头 / 方法白名单 / framing / 路径穿越 / CSRF / 注册登录 / 开放重定向 / Host 头 / 静态资源 / 定向锁定 / 全局闸门 / 评论 XSS+越权 |
| probe 2 | 原始报文（TAB/反斜杠/%09/long URL）/ XFF 与 XFP / 定向锁定复现 / 方法语义 / Cookie 健壮性 / 评论作者与越权（按 DB 状态核对）/ 写路径基准 |
| probe 3 | 带会话静态请求的 `last_seen` 写入证据 / scrypt 单请求成本（~94ms CPU）/ 文章目录 1→5000 文件的 `GET /` 线性劣化 / 全局登录闸门复现 |
| probe 4 | Markdown 净化 9 类载荷 / 不可读文章的错误处理 / 昵称转义 / 评论长度边界 |
| probe 5 | 净化结果 HTMLParser 实证（零可执行构造）/ 昵称属性逃逸实证 / 越权按 DB 状态 / 评论边界 |
| probe 6 | 行内代码二次转义实证 / 昵称属性解析实证 / 可信 HTTPS 代理下的 Secure+HSTS / 会话轮换与登出语义 / robots+sitemap |
| probe 7 | 评论深度硬约束（3 层通过、第 4 层拒）/ 文章评论配额不可逆实证 |
| probe 8 | 慢正文 + 慢请求头 → 2 socket 造成 30.4s / 31.0s 全站不可用 |

关键实测数字回顾：`Retry-After=59`；全局闸门 200 次失败后无关用户被拒；`last_seen` 33ms 内被改写；`5001` 文件时 `GET /` 35.49ms；配额封顶后涂黑 10 条仍 429；慢连接 30.4s/31.0s；`ruff` 58 条；仓库 `tests/`、`docs/`、`README`、`.git` 均不存在。

---

# 修复记录（本次已实施）

原则：**最小改动、不破坏分层、不引入新依赖**。每条都有自动化测试或冒烟复测背书。
运行验证：`python -m unittest discover -s tests -t .` → **82 passed**；`ruff check .` → **0 error**。

## 高危（P1）

| # | 修复 | 位置 | 验证 |
|---|---|---|---|
| P1-A/P1-B | 登录闸门改为**验证优先**：闸门命中时照常校验密码，正确即放行并由 `complete_login_success()` 清空该邮箱失败流水（当场自解封）；密码错误才返回冷却提示，且**补记**这次失败（避免"拒绝即不记账"把计数冻住）。全站闸门同理，不再阻断任何正确凭据 | `modules/auth/routes.py:_try_login`、`db/auth.py`（`record_login_attempt` 由死代码转为在用） | `tests/test_auth_lockout.py` 3 个用例；冒烟：错误密码第 6 次触发闸门后，正确密码仍 302、失败流水归零 |
| P1-C | 慢连接耗尽 worker：**部署层修复**（不改 worker_class，避免改变并发模型）。新增 `deploy/nginx.conf.example`，强制 `client_header_timeout/client_body_timeout/send_timeout` + `limit_conn`；README 说明"为什么它必需"。另修掉"暴露告警只看配置、不看实际 bind"导致的假阴性/假阳性 | `deploy/nginx.conf.example`、`README.md`、`core/config.py:set_effective_bind`、`app.py:warn_on_exposed_bind`、`run.py` | 冒烟：`config.host=127.0.0.1` + `--bind 0.0.0.0` → 现在会出现告警（旧版静默）；`--bind 127.0.0.1` + `config.host=0.0.0.0` → 不再误报 |
| P1-D | 会话 `last_seen` 刷新按 `LAST_SEEN_REFRESH_SECONDS = 300` 节流：带会话的静态资源请求不再每个都取跨进程写锁 | `db/session.py:get_session_user` | `tests/test_auth_lockout.py::test_last_seen_refresh_is_throttled`；冒烟：20 次带会话静态请求后 `last_seen` 字节级不变 |

## 中危（P2）

| # | 修复 | 位置 | 验证 |
|---|---|---|---|
| P2-A | 文章评论配额只统计 `is_deleted = 0`：**涂黑即释放名额**，不再是"打满即永久封禁"（全项目没有 DELETE 评论的路径，所以必须在计数侧修） | `db/comment_rate.py:try_post_comment` | `tests/test_comments.py::test_quota_is_released_by_redaction` |
| P2-B | 文章 / 自定义页面目录扫描加 2 秒节流（`_SCAN_INTERVAL_SECONDS`），把"每请求 O(文件数) stat"变成"最多每 2 秒一次"；启动钩子仍强制全量重扫 | `modules/blog/logic.py`、`modules/pages/logic.py` | `tests/test_core_contract.py`（内容变更可见性）；此前实测 5000 文件时 `GET /` 35ms |
| P2-C | 多进程日志：默认改用 `WatchedFileHandler` + 外部 logrotate（新增 `[logging].rotate`，默认 `false`）；新增**在线备份** `python run.py --backup [--backup-to PATH]`（SQLite backup API + `quick_check`，拒绝覆盖已有文件）；新增 systemd 单元 | `core/logging_config.py`、`core/lifespan.py`、`db/backup.py`、`run.py`、`deploy/*` | 冒烟：备份 `quick_check=ok`、7 表、用户数一致；重复目标被拒并返回非零码 |

## 低危 / 卫生项（P3）

| 原编号 | 修复 | 位置 |
|---|---|---|
| 3-1 | 暴露告警使用**实际** bind（见 P1-C 行） | `app.py`、`core/config.py`、`run.py` |
| 3-2 | `RETENTION_DAYS` 覆盖问题消除：拆成 `RETENTION_DAYS=30`（登录流水）与 `REGISTER_RETENTION_DAYS=7`（注册流水） | `db/auth.py` |
| 3-3 | 删除 10 处死代码：`count_email_failures`/`count_ip_failures`/`count_global_recent_failures`、`db.create_comment`、`clear_session_cookie`、`theme_cookie_header`、`Response.set_cookie`/`delete_cookie`（顺带删掉"模块可自行拼 Set-Cookie"的旁路与 `Response.cookies` 字段）。`record_login_attempt`/`reset_state`/`reset_environment`/`stylesheet_is_same_origin` 由新测试转正 | `db/auth.py`、`db/comment.py`、`core/security.py`、`core/http.py`、`db/__init__.py` |
| 3-4 | 行内代码只转义一次：`escape_raw_html_outside_code` 现在识别反引号 code span（含 CommonMark 同长闭合规则），`` `<div>` `` 渲染为可见的 `<div>` 而不是 `&lt;div&gt;` | `core/markdown.py` |
| 3-5 | i18n 补齐：评论错误提示、403/500 页、后台页全部走 `t()`（新增 25 个键 × 3 语言）；模块不再产出面向用户的英文串（评论 outcome → key 的映射集中在路由层） | `modules/blog/logic.py`、`modules/blog/routes.py`、`modules/system/routes.py`、`templates/admin/index.html`、`i18n/*.toml` |
| 3-6 | `/login`、`/register`、`/logout` 响应改 `Cache-Control: no-store`（含 CSRF 令牌的页面不允许共享缓存复用） | `modules/auth/routes.py` |
| 3-7 | 口令策略：新增 `core.security.is_weak_password()`（常见弱口令表 / 单一字符重复 / 纯数字 / 与邮箱用户名或昵称相同），注册与改密**共用同一判定**；非法时返回 i18n 提示 | `core/security.py`、`modules/auth/routes.py`、`modules/users/routes.py` |
| 3-8 | 协议正确性：`OPTIONS`/`TRACE` 进入方法白名单并由**调度器**回 `405 + Allow`（不再是无 Allow 的早期错误）；缺 `Content-Length` 的判定从请求解析阶段移到"路由命中之后"，因此 `DELETE /` 得到 `405 + Allow` 而真正的表单方法才得 411 | `core/http.py`、`core/routing.py` |
| 3-9 | `[params]` 的 `nav/social/projects` 的 `url`/`icon` 纳入与 `[static]` **同一套** URL 校验（额外允许 `mailto:`，因为仓库自带 email 图标） | `core/config.py` |
| 3-10 | 新增 `pyproject.toml`（ruff 配置，纯开发工具）并把告警从 58 条清到 **0**（含未用导入、未用 noqa、`__all__`/`__slots__` 排序、`zip(strict=)`、三元表达式等） | `pyproject.toml` + 全仓 |
| 3-12 | 新增启动告警：`admin_user_id` 指向不存在/已注销用户、或未配置管理员时点名提示 | `app.py:warn_about_missing_admin` |
| 3-13 | 隐藏上游 `Server: gunicorn`（`proxy_hide_header Server`）与健康检查（`/healthz` → `/robots.txt`），不新增应用端点 | `deploy/nginx.conf.example` |
| 3-16/3-17 | 补齐**测试套件**（7 个文件、82 个用例，仅标准库 `unittest`）：架构守卫、Core 契约、评论、登录闸门与口令、日志与代理信任、文档/配置/i18n 一致性、样式漂移。并加"注释里点名的 tests/*.py 必须存在"的自守卫，杜绝再次"文档说谎" | `tests/*` |
| 3-18 | 补齐交付件：`README.md`（部署/备份/排障/安全模型）、`docs/CONFIGURATION.md`（逐项配置）、`deploy/`（Nginx/systemd/logrotate）、`pyproject.toml` | 仓库根 |
| — | `config.example.toml` 的安全默认值：`[server].host` 由 `0.0.0.0` 改为 `127.0.0.1`；`config.toml` 的 `0.0.0.0` **按你的开发需要保留**（生产改回回环） | `config.example.toml` |

## 明确**不做**的取舍（连同理由）

1. **不把 `worker_class` 改成 `gthread`**：写路径受全局 flock 串行化限制，加线程对写无帮助，
   却会改变既有并发模型；慢连接在 Nginx 层解决（`limit_conn` + `client_*_timeout`）更彻底。
2. **不给静态资源加内容缓存**：收益是省掉一次读盘 + sha256（几十微秒），代价是再引入一层
   缓存一致性（mtime/size 比对、失效、内存上限）。在个人站规模上不划算，保留"每次读盘并算 ETag"的简单实现。
3. **不把 `created_at`/`nickname_changed_at` 改成带时区**：那是数据格式迁移，会改变历史语义
   （在线库需要迁移脚本），不属于缺陷修复。
4. **不删 `style.example.css`**：它可能已被用于 Nginx 侧的自定义样式；只在文档里说明"与内置
   `elenvind/static/css/style.css` 当前内容相同，自定义请以一处为准"。
5. **不引入邮件/找回密码**：会打破"标准库 + 4 个依赖"的约束。改为在 README 的排障表里给出
   "如何清空某邮箱的失败流水"的可执行做法，并保证正确密码永远可用。
6. **不改 `version.stage = "dev"`**：发布策略由站长决定。

## 回归验证快照

```
$ python -m unittest discover -s tests -t .          → Ran 82 tests ... OK
$ ruff check .                                       → All checks passed!
```

冒烟（真实 Gunicorn，2 workers，中文站点）：
`/login` `no-store` ✓、缺 CL 的 POST → 411 ✓、`DELETE /` → 405+Allow ✓、`OPTIONS /` → 405+Allow ✓、
弱口令被拒且提示中文 ✓、闸门命中后正确密码仍 302 ✓、失败流水归零 ✓、
20 次带会话静态请求 `last_seen` 不变 ✓、行内代码 `&lt;div&gt;`（无二次转义）✓、
空/超长评论提示中文 ✓、`--bind 0.0.0.0` 触发暴露告警 ✓、`--backup` `quick_check=ok` ✓。
