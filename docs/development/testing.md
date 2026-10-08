# 测试体系

> **适用读者**：要跑测试、加测试、或想知道"这条约束靠什么保证"的人。
> **一句话**：标准库 `unittest`，不 mock 数据库、不碰真实数据，需要并发就用**真进程**。

---

## 1. 原则

- **只用标准库 `unittest`**（`requirements.txt` 里没有任何测试依赖）；
- **数据隔离**：每个用例一个临时目录 + 临时 SQLite 文件 + 临时内容目录，
  你的真实 `sqlite.db` / `articles/` / `custom_pages/` 永不被触碰；
- **不 mock 数据库**：并发、锁、迁移这些正是被测对象，mock 掉就等于什么都没测；
- **需要并发就用真 `multiprocessing`（fork）**，而不是线程或假对象；
- **同步**：测试与业务一样是普通 `def`（若在生产代码里写了 `async`，会有守卫测试变红）。

---

## 2. 怎么跑

```bash
# 全部（约 140 s，接近 800 条）
python -m unittest discover -s tests -t .

# 单模块（加 -v 看每个用例名）
python -m unittest tests.test_http -v

# 三组守卫
python -m unittest tests.test_architecture -v        # 分层与依赖方向
python -m unittest tests.test_core_contract -v       # 安全契约（模块不得绕过 Core）
python -m unittest tests.test_doc_consistency -v     # 文档与代码一致

# 真实进程的 C0 测试
python -m unittest tests.test_c0_write_tx -v
python -m unittest tests.test_c0_concurrency -v

# 端到端：真的起 Gunicorn，打真实 HTTP
python smoke_driver.py                               # 61 项检查
```

`smoke_driver.py` 会用一个临时数据库起真实 Gunicorn（2 worker），跑完自动停：
启动 → 首页/文章/自定义页面/robots/sitemap → 静态资源与路径穿越 → 主题 → 404/405 →
注册/登录/会话 Cookie 属性 → 个人中心 → 评论（发表/回复/**编辑**/**涂黑删除**）→ 改密/注销 →
413/411/415 → Host 头投毒。部署后想快速自检，直接跑它。

---

## 3. 测试模块清单

| 模块 | 覆盖 |
|---|---|
| `test_architecture` | **架构守卫**：Core↛Modules、Modules↛App、模块互不依赖、无环、模块独立可导入、公开入口形状、无旧 `features/` 路径 |
| `test_core_contract` | **Core Contract**：模块不得重造 CSRF/Cookie/密码/模板环境/Markdown，不得执行 SQL、不得开连接；写 SQL 必须落入 `write_tx()` |
| `test_doc_consistency` | 文档与代码一致：会话天数、`max_comment_depth=0` 语义、`admin_user_id`、引用的路径/符号/图标存在、config 键集合配平、索引列出所有文档 |
| `test_c0_write_tx` | 单进程写事务语义：提交/回滚/嵌套拒绝/锁覆盖整个事务/连接设置/WAL |
| `test_login_rate_limit` | **登录限流安全语义**：并发不超发（8 线程 / 阈值 3）、占位记账、被拦不写行、渐进 backoff 会过期（不是 24h 硬锁）、`Retry-After` 头、时间索引、v4→v5 迁移 |
| `test_markdown_security` | **Markdown/XSS 回归**：协议相对 URL 被拒、scheme 混淆（实体/TAB/百分号/大小写）、`style=`/`on*=` 剥离、危险容器连内容删除、缩进误判边界下的属性剥离、围栏与注释不可藏 payload |
| `test_hardening` | **加固回归**：CSP 默认不含远程通配、非回环监听 + 信任转发头会告警、静态软链指向隐藏文件被拒、会话轮换/登出只杀当前会话 |
| `test_c0_concurrency` | **真多进程**：并发写不丢数据、临界区不重叠、`SIGKILL` 恢复杂、并发 `init_db()`、非幂等迁移只执行一次、不同库不同锁 |
| `test_http` | 请求边界：Content-Length、类型白名单、体积上限、HEAD、Unicode、坏 UTF-8、405/Allow、安全头 |
| `test_wsgi` | PEP 3333：同步 callable、`start_response`、响应头 native str + latin-1、`SCRIPT_NAME`、代理 scheme 判定、无 async/ASGI 残留 |
| `test_wsgi` / `test_logging_proxy` | 生产入口 `elenvind.wsgi` 的 startup 顺序与符号 |
| `test_logging_proxy` | 日志脱敏（真实流程断言不含密码/令牌/Cookie）+ **客户端 IP 信任边界**（伪造 XFF 不能绕限流） |
| `test_runtime_logging` | 运行期日志详细度：访问日志字段、写事务明细、审计事件、控制字符转义 |
| `test_security` / `test_security_headers` / `test_security_regression` | 密码哈希与 rehash、CSP/Permissions-Policy/HSTS、历史漏洞回归（开放重定向、Host 投毒、500 页不泄漏） |
| `test_auth` / `test_cookie_policy` / `test_session_expiry` | 注册/登录/限流/删号、Cookie 属性与 `__Host-` 前缀、会话绝对/滑动过期与清理 |
| `test_comments` / `test_comments_extra` | 评论树、深度上限、涂黑删除、限流、权限（作者/管理员）、N+1 防护 |
| `test_comment_edit` | **涂黑与编辑**：原文从库里消失（含库文件明文扫描）、等长上限、幂等、v6 迁移涂黑历史行、无恢复入口（404/405）、渲染对管理员同样涂黑、编辑表单预填（`?edit=`）、只有作者能改（管理员也不行）、已涂黑不可编辑、空/超长/CSRF/跨文章拒绝、编辑不动树位置 |
| `test_articles` / `test_markdown` / `test_fuzz` | 内容索引与缓存原子性、Markdown 渲染契约、净化白名单、固定种子的 fuzz |
| `test_seo` / `test_config_behavior` / `test_config_i18n` | robots/sitemap（Host 不可投毒）、配置校验与运行期行为、i18n 表完整性 |
| `test_styles` | 样式表与模板的类名漂移守卫（模板用了未定义的 class 就失败） |
| `test_integration_coldstart` | 冷启动全流程（配置 → 日志 → 模板 → 建库 → 缓存），含"配置非法必须拒绝启动" |

---

## 4. 三组守卫分别在拦什么

| 守卫 | 判定方式 | 例子 |
|---|---|---|
| `test_architecture` | **AST** 解析 import 结构 + 有向图找环 | 模块里写了 `from ...modules.blog import logic` → 红 |
| `test_core_contract` | **AST** 找 SQL 调用/危险构造 + **运行时**跑真实流程 | 模块里写了 `conn.execute("INSERT …")`、自造 `hash_password` → 红 |
| `test_doc_consistency` | 文本断言 vs **代码常量** | 文档写"会话 7 天过期"、引用不存在的脚本/符号 → 红 |

为什么用 AST 而不是字符串匹配：注释与文档里出现 `sqlite3`、`BEGIN IMMEDIATE`
是正常表述，字符串匹配会误报；AST 只看真实的 import / 调用 / 定义。

---

## 5. C0 多进程测试为什么不 mock

`test_c0_concurrency` 用 `multiprocessing.get_context("fork")` 起**真进程**，每个进程
独立打开数据库、走 `write_tx()`：

| 用例 | 做法 | 断言 |
|---|---|---|
| 并发写不丢数据 | N=4 进程各写一批 | 行数正确、无 `database is locked`、`integrity_check=ok` |
| 临界区不重叠 | 每进程持锁 sleep，记录进入/退出时刻 | 任意两个区间不相交 |
| `SIGKILL` 恢复 | 子进程在事务中不提交，父进程 `os.kill(SIGKILL)` | 锁自动释放、未提交行不存在、后续写入成功 |
| 并发初始化 | 4 进程同时 `init_db()` | 全部成功、`user_version` 正确、无 `*_legacy` 残留 |
| 非幂等迁移 | 自定义迁移里 `CREATE TABLE`（第二次必失败） | 迁移恰好执行一次 |

这些必须真进程：线程共享 fd，`flock` 的表现完全不同；mock 则什么也证明不了。

---

## 6. 测试夹具（`tests/support.py`）

```python
from tests.support import ElenvindTestCase

class MyTests(ElenvindTestCase):
    def test_something(self):
        self.write_article("hello", "# Body", {"title": "Hello"})   # 写内容 + 推进 mtime
        csrf = self.fetch_csrf()                                    # 取 CSRF Cookie
        session, _ = self.login_ok("a@example.com", "pw")            # 注册/登录辅助
        r = self.app.request("GET", "/hello", cookies=self.app_cookies(session, csrf))
        self.assertEqual(r.status, 200)
```

| 工具 | 用途 |
|---|---|
| `self.app.request(...)` / `raw_request(...)` | 走完整 WSGI 链路的请求（自带 Cookie 罐可选） |
| `build_environ()` / `call_wsgi()` | 驱动独立 `App` 实例（不经过夹具） |
| `StreamInput` | 模拟短读/截断/空读的请求体 |
| `write_article()` / `write_page()` | 造内容文件（自动推进 mtime，避免文件系统精度导致缓存不失效） |
| `fetch_csrf()` / `csrf_cookies()` / `app_cookies()` | 令牌与 Cookie 罐（自动适配 `cookie_prefix`） |
| `self._config` | 覆盖配置（如把限流阈值调小）；用完由夹具恢复 |
| 应用日志捕获 | 在 `"elenvind"` logger 上挂 handler（**不要挂 root**：`setup_logging()` 会摘掉 root 的 handler） |

---

## 7. 加测试的约定

1. **放对文件**：按上面清单的表意归属；新能力就新开 `test_<name>.py` 并在
   [../README.md](../README.md) 索引与本文档登记。
2. **断言行为，不断言实现**：例如断言"限流命中返回 429 + 提示文案"，而不是断言某函数被调过。
3. **时间相关的测试别睡太久**：用配置把窗口/阈值调小，或 monkeypatch 阈值常量
   （例：`db_base._SLOW_WRITE_SECONDS = 0.01`），用完恢复。
4. **随机要固定种子**（fuzz 用固定 seed，失败可复现）。
5. **不要为了测试放宽生产代码**：为了可测而加开关等于把测试需求塞进产品。
6. **新模块必须登记**：`tests/test_architecture.py::MODULE_ENTRYPOINTS` 与文档（守卫会检查）。
7. **改配置键**：同步 `config.example.toml` 与 [../CONFIGURATION.md](../CONFIGURATION.md)
   （键集合配平守卫会红）。

---

## 8. 常见坑

| 现象 | 原因 / 解法 |
|---|---|
| 缓存测试偶发失败 | 同一文件系统 tick 内连续写得到相同 mtime；用 `write_article(touch=True)` |
| 日志测试抓不到记录 | `setup_logging()` 按设计会摘掉 root 的 handler；把 handler 挂到 `"elenvind"` logger |
| `RuntimeError: write_tx() 不可嵌套` | 用例里在 `write_tx()` 块内又调了写函数（如 `db_prune.prune()`）；移到块外 |
| 多进程用例在非 POSIX 上跳过 | `flock` 只在 POSIX 存在；Windows 上会 `skip`（本项目部署目标是 Linux） |
| 测试后残留 `logs/app.log` | 夹具使用临时目录；若手动起过服务，`logs/` 已进 `.gitignore` |
