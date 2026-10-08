# 安全模型

> **适用读者**：部署的人、改安全相关代码的人、做安全审计的人。
> **一句话**：安全能力在 Core 实现一次、由调度器默认施加；信任边界只有一处
> （`[server].trusted_proxies`），业务模块既不需要、也不允许重复实现。

---

## 1. 威胁模型

| 攻击者 | 典型行为 | 对应防线 |
|---|---|---|
| 匿名陌生人 | 扫路径、探测框架版本、Host 头投毒、路径穿越取文件 | 404/405 统一响应、不输出框架版本、绝对 URL 只来自 `site_url`、静态服务限制在 `elenvind/static/` 内 |
| 自动化爆破 | 撞密码、批量注册、刷评论 | 登录三桶限流（邮箱/IP/全局）、注册 IP 限流、评论用户/IP 限流 |
| 内容注入者 | 在评论/文章里塞脚本、事件属性、危险协议 | Markdown 白名单净化 + 模板自动转义 + CSP `script-src 'none'` |
| 已登录的普通用户 | 改别人的评论、越权访问管理员页面、伪造身份 | 路由声明式授权（`auth` / `permission`）、评论所有权校验、会话服务端存储 |
| 中间人 / 同网段 | 窃取 Cookie、降级到 HTTP | TLS（部署侧）、`Secure` Cookie、HSTS（可开）、`SameSite=Lax` |
| 拿不到 shell 但能发请求的人 | 伪造 `X-Forwarded-*` 冒充代理、伪造客户端 IP 绕过 IP 限流 | **信任边界**：只有直连对端在 `trusted_proxies` 内才看转发头；客户端 IP 从右往左跳过受信跳 |

**不在威胁模型内**：拿到服务器 shell / 数据库文件读写权限的攻击者；被入侵的反向代理本身；
共享主机上同一 Unix 用户的横向移动。这些属于操作系统与部署层，见 §10。

---

## 2. 信任边界（最重要的一节）

```
浏览器 ──TLS──► Nginx（受信代理，终止 TLS）──明文 HTTP──► Gunicorn（监听 127.0.0.1）──► 应用
```

应用只在**直连对端**属于 `[server].trusted_proxies`（默认 `127.0.0.1`、`::1`）时才采信：

| 头 | 用途 | 取值规则 |
|---|---|---|
| `X-Forwarded-For` | 客户端 IP（限流、审计） | 从**最右**往左找第一个**非受信**地址（代理链每跳都要登记在 `trusted_proxies`） |
| `X-Forwarded-Proto` | `https` 判定（`Secure` Cookie、HSTS） | 取最右的有效值 |
| `X-Real-IP` | —— | **一律不采信** |
| `Host` | —— | **不用于生成任何绝对 URL**（`site_url` 是唯一来源，防 Host 头投毒） |

为什么取最右而不是最左：代理若使用"追加"语义（例如 `$proxy_add_x_forwarded_for`），
客户端自带的头会被原样保留在**左侧**；取最左就等于允许任何人自选客户端 IP，
从而绕过按 IP 计数的登录/注册/评论限流。取最右（并跳过受信代理地址）在"覆盖"与
"追加"两种代理配置下都拿到真实来源。

**唯一来源**：`run.py` 把同一份 `trusted_proxies` 交给 Gunicorn 的
`forwarded_allow_ips`，应用自己用同一份白名单判定 —— 不存在"HTTP 服务器一份、
应用一份"的漂移。直接用 Gunicorn CLI 启动时要手动传一致的 `--forwarded-allow-ips`。

**代理侧正确写法**（覆盖，不要追加）：

```nginx
proxy_set_header X-Forwarded-For   $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Host              $host;
```

完整示例见 [nginx.conf.example](nginx.conf.example)；`trusted_proxies` 的配置说明见
[CONFIGURATION.md](CONFIGURATION.md#代理信任边界务必理解)。

---

## 3. 认证与会话

| 项 | 实现 |
|---|---|
| 密码 | `hashlib.scrypt`，哈希自描述（含参数），登录时参数过期会**透明 rehash** |
| 用户枚举 | 登录失败一律同一文案；账号不存在时也做等量哈希校验（弱化计时侧信道） |
| 会话存储 | **服务端**随机 token 存在 SQLite（不是 JWT、不是可解析的凭据） |
| 会话过期 | 绝对 30 天 + 空闲 15 天（任一超时即失效并当场删除）；配置键见 CONFIGURATION |
| 会话轮换 | 登录成功时轮换 token（旧 token 立即失效） |
| 改密 / 注销 / 删号 | 该账号**全部会话立即失效**，并让浏览器 Cookie 立刻过期 |
| 账号注销（自我） | 逻辑删除：昵称占位、邮箱换随机不可注册值、会话清空，评论归属保留显示为占位 |
| Cookie 属性 | `HttpOnly` + `SameSite=Lax` + `Path=/`；HTTPS 请求自动 `Secure`；可选 `__Host-` 前缀（`cookie_prefix`） |
| 最小化 | 应用端口只应监听 `127.0.0.1`（`[server].host`），由反代对外 |

---

## 4. CSRF 与"状态变更"

- 机制：**double-submit cookie**。令牌随机、随表单下发（`{{ csrf_input() }}`），
  提交时比对 Cookie 与表单值；实现在 Core 一处（`core/csrf.py`），由调度器对
  **所有非安全方法**默认施加。
- 失败响应：`400`（不泄漏任何上下文）。
- 状态变更只由 POST（+ 令牌）触发：`GET /logout` 只显示确认页，不销毁会话。
- 例外（**刻意且已文档化**）：`/theme?mode=dark` 用 GET 写"显示偏好" Cookie。
  它不改变任何服务端状态、不影响他人、无可用后果，而被第三方链接切换配色这一
  风险远小于"最简单的 `<a>` 链接失效"的代价。除此之外任何写操作都不得走 GET。
- 回跳地址（`?next=`）只接受站内路径（拒绝 `//`、反斜杠、CR/LF/TAB），防开放重定向。

---

## 5. 授权

```python
@router.route("/settings", methods=["POST"], auth="required")            # 需登录
@router.route("/admin",    methods=["GET"],  auth="required", permission="admin")
@router.route("/<slug>",   methods=["GET"],  fallback=True)              # 兜底
```

| 档位 | 语义 |
|---|---|
| 未声明 | 公开 |
| `auth="required"` | 匿名 GET → `302 /login?next=…`（导航友好）；匿名非 GET → `403`（无跳转可言） |
| `permission="admin"` | 已登录但权限不足 → `403`；未登录 → 同上友好跳转 |
| `fallback=True` | 仅在没有任何声明路由匹配时才参与（`/<slug>` 不会遮蔽 `/login`） |

**失效关闭**：`permission` 只接受已知档位；写错（如 `"amin"`）会**拒绝**而不是放行。
管理员由 `admin_user_id` 指定（可删除/恢复任意评论），不硬编码为 1。

---

## 6. 输入、输出与注入面

| 面 | 防线 |
|---|---|
| 请求体 | 体积上限（默认 1 MB → `413`）、`Content-Length` 必须存在（→ `411`）、类型白名单（→ `415`）、方法白名单（→ `405` + `Allow`） |
| HTML 输出 | Jinja2 自动转义；模块不得用 `|safe` 处理用户数据（有守卫），不得手写 `<script>`/`on*=` |
| Markdown | 唯一入口 `core.markdown.render_markdown()`：白名单标签/属性 + 协议白名单（stdlib `html.parser`，无第三方净化库） |
| 富文本 | `markupsafe.Markup` 只由渲染层产出；模块不自己拼可执行标记 |
| 响应头 | `Location` 含控制字符直接拒绝（防 header 注入）；Cookie 值经白名单/字符校验 |
| 日志 | 请求路径中的控制字符转义（防伪造日志行） |
| 静态文件 | 解析后必须仍在 `elenvind/static/` 内；`..`、`%2e%2e`、反斜杠、越界符号链接、隐藏文件全部 404 |
| 错误页 | 500 页只显示固定文案，异常对象**不进**渲染上下文；堆栈只进日志 |
| SQL | 参数化查询；模块不得拼 SQL、不得自己开连接（写只能走 `write_tx()`） |

---

## 7. 安全响应头（Core 对每个响应统一注入）

| 头 | 默认值 |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | camera / microphone / geolocation / payment / usb / … 全部 `()` |
| `Strict-Transport-Security` | 仅在 `hsts_enabled = true` **且**请求确为 HTTPS 时下发（默认关闭） |

- **外链图片/视频默认放行**（`img-src 'self' data: http: https:`、`media-src 'self' http: https:`）：
  这是产品取舍 —— 配图/视频走外链，单机静态服务不必承担大文件带宽与磁盘（内置 `static/`
  只放 favicon、logo 等小而特殊的资源）。代价是访问者会向第三方发起请求（IP/UA 可见）；
  想收紧就在 `[security.csp]` 里**显式列出主机**
  （例：`img-src = ["'self'", "data:", "https://cdn.example.com"]`）——
  守卫只允许收窄，裸 `http:` / `https:` / `*` 会被 `tests/test_doc_consistency.py` 拒绝；
- 协议相对 URL（`//host/x`）在 Markdown 与净化器层都被拒绝 —— 否则内容作者不写
  `http(s)://` 也能引入任意第三方资源；
- **404 / 403 / 500 也带全套头**（守卫与真实请求都验证过）；
- `script-src 'none'` 是 Zero-JS 的直接落地：模板里没有 `<script>`、没有内联事件属性；
- `style-src` 保留 `'unsafe-inline'` 是**刻意**的：CSP 的 `style-src` 同时管
  `<style>` 元素与 `style=` 属性，而首页 hero 用 `style="background-image:url(…)"`。
  去掉它会让 hero 图片静默消失。注入风险在配置层收口：`[static].hero` 必须通过字符与
  协议白名单（禁引号/括号/反斜杠/空白，只允许 http(s) 或站内路径）；
- HSTS 默认关闭的理由：站点若还在明文 HTTP 上，下发 HSTS 会把访客锁在门外。

细节与全部可调项见 [CONFIGURATION.md](CONFIGURATION.md#security-安全响应头)。

---

## 8. 限流与滥用防护

| 维度 | 默认 | 目的 |
|---|---|---|
| 登录 · 单邮箱 | 5 次 / 24 h 窗口 | 定向爆破主防线 |
| 登录 · 单 IP | 20 次 / 15 min 窗口 | 辅助防线 |
| 登录 · 全站 | 200 次 / 15 min 窗口 | 分布式爆破最后闸门 |
| 注册 · 单 IP | 5 次 / 1 h | 防批量注册 |
| 评论 · 单用户 / 单 IP | 5 / 10 条每分钟 | 防刷屏 |

- 计数落库（不是内存），多 worker 共享同一份计数；
- **判定与记账在同一个写事务里**（`db.reserve_login_attempt`）：并发请求不可能同时
  读到"还没到阈值"而超发（旧实现是"读计数 → scrypt → 记账"，存在 TOCTOU）；
- 达到阈值后是**渐进式冷却**（`60s × 2^(超出次数)`，封顶 24h）而不是一刀切硬锁：
  偶尔打错的正常用户等约一分钟就能重试，持续爆破的等待时间迅速增长；
  被限流时会下发 `Retry-After` 并在文案里给出真实等待时长；
- 昂贵的 scrypt 校验**不持有 SQLite 写锁**（短事务：占位 → 释放 → 校验 → 收尾）；
- 登录成功后该账号的失败流水**立即清除**，正常用户不会被历史失败拖累；
- IP 计数的正确性依赖 §2 的信任边界：代理头配置错误 = IP 限流形同虚设；
- 误锁处理（家庭 NAT 被拖累等）见 [OPS_GUIDE.md](OPS_GUIDE.md#五限流策略与误锁处理)。

**账号删除的语义（身份删除 ≠ 内容删除）**——站点在删号页面明确列出：

| 删除 | 保留（匿名化） |
|---|---|
| 登录凭据（密码）、邮箱地址、昵称、**全部会话**（所有设备登出） | 历史评论**正文**仍然可见，但不再与账号关联：显示为配置的占位昵称（如"已远行"），**不再显示用户编号**（避免用稳定 id 反查同一个人） |
| 该邮箱的登录失败流水（含 IP） | 用户行本身（`is_deleted = 1`，保留 `id`/`created_at` 供评论归属与审计） |

- 具体实现：`db.delete_user()`（逻辑删除 + 邮箱替换为不可注册占位值）、
  `db.clear_login_attempts(原邮箱)`、`invalidate_user_sessions(...)`；
- 是否**连评论正文一起删除**属于产品决策（当前选择保留，以维持讨论串完整性）；
  若政策要求"彻底删除"，需要新增一个显式的正文清除入口并写进本表。

**已知取舍**：注册失败不区分"邮箱已占用"与其它原因（防账号枚举），代价是文案不精确。

---

### 13.1 登录的 CPU 成本（实测，未修改 scrypt 参数）

`scrypt` 是 CPU 密集的（自描述哈希 `ln=15,r=8,p=1`）。在**同步 WSGI + Gunicorn
sync worker** 下，每个并发登录占用一个 worker 直到哈希完成，因此并发升高时表现为
排队（而不是失败）。真实压测（2 worker，临时数据库，全部 302 成功、无 5xx）：

| 并发 | p50 | p95 | p99 | max | 墙钟 | 吞吐 |
|---|---|---|---|---|---|---|
| 1 | 84 ms | 84 ms | 84 ms | 84 ms | 86 ms | ~12/s |
| 5 | 167 ms | 168 ms | 168 ms | 248 ms | 250 ms | ~20/s |
| 20 | 453 ms | 815 ms | 815 ms | 820 ms | 826 ms | ~24/s |
| 50 | 1064 ms | 1954 ms | 2035 ms | 2038 ms | 2054 ms | ~24/s |

**结论与处置（刻意不改密码参数）**：

- 吞吐上限 ≈ `workers / 单次哈希耗时`（实测约 24 次登录/秒 @2 worker）；并发超过
  worker 数后延迟线性上升，这是同步模型的正常排队行为；
- **写锁不参与**：scrypt 在校验前已经释放 SQLite 写锁（短事务占位 → 释放 → 校验），
  因此登录风暴不会拖住评论/注册等写入；
- 登录限流（邮箱/IP/全局三桶 + 渐进冷却）会把"爆破型并发"压在阈值内，
  正常游客的偶发并发（个位数）延迟仍在百毫秒级；
- 若站点真的会出现登录突发：**加 worker**（登录是 CPU-bound，加 worker 有效）
  或在反代层加限速；**不要**为了压测数字调低 scrypt 参数；
- 注册（`/register`）同样要付一次哈希，属同类成本。

## 9. 日志安全

| 记 | 不记 |
|---|---|
| 方法、路径（**不含查询串**）、状态码、耗时、字节数、客户端 IP、用户 id、worker pid | 密码、密码哈希、会话/CSRF 令牌、Cookie、`Set-Cookie`、请求体、Referer、User-Agent、邮箱（PII） |
| 审计事件：登录成/败、注册成/拒、改密、删号、注销、评论发表/审核 | 任何可用于重放的凭据 |

- 请求路径里的控制字符会转义成 `\xNN`（防 `%0a` 伪造日志行）；
- 访问日志与写事务日志由 Core 统一记录，模块只记业务事件；
- 部署侧：`logs/` 仅服务账号可读写（见 [OPS_GUIDE.md](OPS_GUIDE.md#八日常安全检查清单)）；
  日志含 IP，按你们适用的隐私要求设定保留期与归档；
- 有测试对**整条真实流程**做脱敏断言（`tests/test_logging_proxy.py` +
  `tests/test_runtime_logging.py`）。

---

## 10. 部署侧加固清单

- [ ] `[server].host = "127.0.0.1"`，只让反代回源；用防火墙确保应用端口不可从公网直连
      （否则攻击者可绕过 Nginx 直接发请求，并伪造 `X-Forwarded-*` —— 直连时它们会被忽略，
      但 TLS、HSTS、限速都失效）
- [ ] 反代的 `X-Forwarded-For` 用**覆盖**语义（`$remote_addr`），不要 `$proxy_add_x_forwarded_for`
- [ ] `trusted_proxies` 只写反代地址（多级代理写全每一跳）；**绝不写公网地址或网段**
- [ ] 直接用 Gunicorn CLI 启动时，`--forwarded-allow-ips` 与 `trusted_proxies` 保持一致
- [ ] 全站 HTTPS，`certbot` 自动续期；确认浏览器里 session Cookie 带 `Secure`
- [ ] 明确要用 HTTPS-only 后，再考虑 `cookie_prefix = true`（`__Host-` 前缀）与 `hsts_enabled = true`
- [ ] Nginx：`server_tokens off;`，并在 `location /` 里 `proxy_hide_header Server;`
      （默认响应头会写 `Server: gunicorn`；它不含版本号，但没必要公告技术栈）
- [ ] systemd：`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=strict` +
      `ReadWritePaths=` 指向站点目录（示例见 [DEPLOYMENT.md](DEPLOYMENT.md)）
- [ ] 数据库与锁文件权限仅服务账号可读写；备份用 `sqlite3 .backup`，备份文件也要限权/加密
- [ ] 定期 `PRAGMA integrity_check` 与 schema 版本核对（见
      [development/database.md](development/database.md#10-备份恢复与巡检)）

### 部署后 30 秒自检

```bash
BASE=https://your.domain

# 1) 安全头齐全（含 404/500 路径）
curl -sSI $BASE/ | grep -iE 'content-security-policy|x-content-type-options|referrer-policy|permissions-policy'
curl -sSI $BASE/no-such-page | grep -i 'x-content-type-options'

# 2) 代理信任：只在受信反代后才有 Secure / HSTS
curl -sS -D - -o /dev/null $BASE/login | grep -i '^set-cookie'      # 期望含 Secure
curl -sSI $BASE/ | grep -i 'strict-transport-security' || echo 'HSTS 未开启（默认）'

# 3) Host 头不可投毒（绝对 URL 只来自 site_url）
curl -sS -H 'Host: evil.example' $BASE/sitemap.xml | head -3

# 4) 静态服务不越界
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/../config.toml"
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/css/../../config.toml"

# 5) 写接口需要令牌：匿名/无令牌 POST 必须被拒
curl -sS -o /dev/null -w '%{http_code}\n' -X POST -d 'content=x' $BASE/article/<slug>/comment
```

### 代码级验证

```bash
python -m unittest tests.test_core_contract -v      # 模块不得绕过/重造安全能力
python -m unittest tests.test_architecture -v       # 分层依赖方向
python -m unittest tests.test_security tests.test_security_headers \
                   tests.test_security_regression tests.test_cookie_policy -v
python -m unittest tests.test_logging_proxy -v      # 脱敏 + 代理信任
python smoke_driver.py                              # 端到端 61 项
```

---

## 11. 已知取舍（写下来，免得被当成疏漏）

| 取舍 | 理由 | 想收紧时怎么做 |
|---|---|---|
| `style-src 'unsafe-inline'` | hero 用 `style=` 属性；去掉会让图片静默消失 | 把 hero 改成类名/`<style>` 或去掉该功能，再删掉 `'unsafe-inline'` |
| `/theme` 用 GET 写偏好 Cookie | 只影响该浏览器自身显示，无服务端影响；换成 POST+令牌会让最简单的链接失效 | 若主题需要服务端存储，改为 POST + CSRF |
| 注册失败不区分原因 | 防账号枚举 | 若接受枚举风险，可换成更具体的文案 |
| `Server: gunicorn`（无版本） | gunicorn 默认行为，应用层无法安全移除 | 反代加 `proxy_hide_header Server;` |
| 访问日志含客户端 IP | 排障与滥用追责必需 | 设置日志保留期/访问控制；如合规要求，可只归档聚合结果 |
| `busy_timeout`（5 s）作为最后兜底 | 协议外写入者（手工 SQL）可能短暂持锁 | 别让别的程序直接写这个库 |

发现的**安全漏洞**请走私有渠道报告（不要开公开 issue），附最小复现；修复以补丁形式
给出（见 [README 的贡献约定](../README.md#license)）。
