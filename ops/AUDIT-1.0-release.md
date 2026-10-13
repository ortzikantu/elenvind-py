# Elenvind 1.0 上线前审查与收束（第三轮）

- 审查对象：本仓库工作树（版本 `v1.0.0`）
- 审查方式：全量代码走查 + 在 `/tmp` 副本上的动态取证（真实 Gunicorn 多 worker、用户态 Nginx、真实 SQLite 文件操作、并发线程、`systemd-analyze`、`nginx -t`）
- 约束：不换依赖、不改分层、不引入新基础设施；只做修复已证实缺陷所必需的最小改动
- 范围外（按约定）：管理员初始化策略、监听地址与 Host 策略、依赖选型、架构重构
- **路径说明**：本文写于目录合并之前（`deploy/` + `docs/` + 示例文件 → `ops/`）。
  文中出现的 `deploy/…`、`docs/CONFIGURATION.md`、`config.example.toml`、
  `style.example.css`、根目录 `AUDIT.md`，现在都在 `ops/` 下；代码路径（`elenvind/…`、
  `tests/…`、`data/…`、`logs/…`）未变。

## 0. RC 最终轮（本轮）增量

第三轮修复落地后，又做了一轮独立核查（基于提交 `d71a843`，工作区干净），确认没有 P0/P1 级的遗留问题，并补齐了 6 条守卫：

| 事项 | 证据 | 处理 |
|---|---|---|
| Nginx 与应用请求体限制的**顺序** | 示例原为 `client_max_body_size 1m` 而注释写"略大一点，让应用返回 413" —— 实际两者**相等**，>1 MiB 的请求由 Nginx 抢先 413，应用侧 413 在反代后观察不到（文档与行为矛盾） | 改为应用先触发（1 MiB）、Nginx `2m` 兜底，注释写明两层分工；新增守卫 `test_nginx_body_limit_leaves_room_for_the_application_limit` |
| 代理读超时余量 | 示例已为 40s（> Gunicorn 默认 30s），没有守卫防止被改回 | 新增 `test_nginx_proxy_read_timeout_exceeds_gunicorn_timeout` |
| 符号链接处理 | `resolve_asset()` 有 `resolve(strict=True)` + `is_relative_to(root)` + 隐藏段二次检查，但**没有测试** | 新增 `test_rejects_symlinks_that_escape_or_hide`（指向外部文件 / 指向隐藏文件 / 目录软链到 `/etc` 三种形态） |
| 日志轮转后能否继续写 | 此前只做过手工验证 | 固化为 `test_operations.py::LogRotation`（真实 handler：写 → 改名 → 再写） |
| 恢复后的库能否读写 | 此前只有手工演练 | 固化为 `test_restored_database_supports_reads_and_writes`（走应用层 `db.configure()` 切换后读 + 写） |
| 失败备份是否会破坏有效备份 | 此前只覆盖"拒绝覆盖" | 新增 sha256 逐字节比对：`test_failed_backup_never_touches_the_existing_copy` |

本轮验证快照（实测）：

```
python -m unittest discover -s tests -t .   → Ran 109 tests ... OK   （0 skipped；上一轮 103）
ruff check .                                → All checks passed!
python -m compileall -q elenvind tests run.py → exit 0
```

## 0.2 第四轮：发布前完整源码审查（HEAD `18648b4`）

对当前检出提交做的一轮独立源码审查（10 762 行一方 Python、16 个模板），结论与增量：

**结论**：未发现 Critical/High/Medium 级缺陷；发现 1 个可观测性/API 缺陷（已修）、4 处注释与实现漂移（已修）、2 处死代码（已修）。

| 发现 | 证据 | 处理 |
|---|---|---|
| `db.DB_PATH` 是 import 时刻的**快照**，`configure()` 后不更新 → 启动日志报告的是默认路径而不是 `config.toml` 指定的路径（实际库文件、锁文件都在正确位置，仅日志与任何使用该名的调用方被误导；`tests/test_operations.py` 也用了它） | 复现：`db.configure("/tmp/x/other.db")` 后 `db.DB_PATH` 不变、`db.connection.DB_PATH` 变；启动日志显示 `<root>/sqlite.db` 而实际文件在 `<root>/data/sqlite.db` | 移除快照导出，改为实时函数 `db.db_path()`；`app.py` 日志与测试改用它；新增 3 条守卫（`DatabasePathAPI`）。修复后启动日志显示 `/tmp/xcheck3/data/sqlite.db` 与实际文件一致 |
| `elenvind/db/session.py` 顶部 docstring 声称"本模块**全是写操作**…统一走 write_tx"，与实现相反（`get_session_user()` 走只读 `connect()`） | 源码对照 | 已修正（并说明读写分路径） |
| `modules/users/routes.py` docstring 引用已删除的 `db.user.update_user_password` 与 `core.session.invalidate_user_sessions` | 源码对照 | 已修正为实际的单事务原语 |
| `db/auth.py` 注释引用"第 209 行"（该行已不存在）、`db/migration.py` 两处引用已删除的 `delete_user_sessions()` / `count_global_recent_failures()` | 源码对照 | 已修正措辞 |
| 死代码：`db.auth.record_login_attempt`、`db.session.delete_user_sessions`（均无任何调用者，属上两轮修复的遗留） | `grep` 引用计数为 0 | 已删除并同步导出面（`migrate()` 保留：它是公开迁移入口，无内部调用者但属运维可用 API） |

**本轮动态验证（新增证据）**

- **多进程并发写入**（8 进程 × 5 用户 + 5 评论，独立临时库）：0.16 s 内 40 用户 + 40 评论，**零异常、零丢失、零重复邮箱、零外键孤儿、`integrity_check=ok`**；8 进程抢注同一邮箱恰好 1 成功 + 7 个 `IntegrityError`。
- **只读路径不占写锁**：在持有 `write_tx()` 的同一线程内用 `connect()` 读成功（若只读路径也取 flock 会自死锁）—— 机制级证明。
- **事务异常**：`write_tx()` 内抛异常 → 行数不变（回滚）→ 锁立即可再次获取；嵌套 `write_tx()` 抛 `RuntimeError`（不死锁）；`init_db()` 重复调用幂等。
- **绕过锁协议的外部写入者**（`sqlite3` CLI）：应用持有 `BEGIN IMMEDIATE` 时 CLI 写入立刻得到 `database is locked`（不损坏、不部分写入），事务结束后可正常写入 —— 说明 flock 失效也不会导致数据损坏（SQLite 自身锁兜底）。
- **只读数据库**（`chmod 444` 三个文件）：写请求 HTTP 500、读请求 HTTP 200，权限恢复后注册即自愈 HTTP 302。
- 启动/停止：2 worker 正常启动（schema v6 / WAL）、9 条路径冒烟 200/404 符合预期、SIGTERM 后残留进程 0、端口释放。

**第四轮结束后的验证快照**：`Ran 112 tests ... OK`（新增 3 条）、`ruff check .` → All checks passed、`compileall` 通过。

## 1. 结论

**没有发现 P0 或 P1。** 共 5 项 P2、6 项 P3，**全部已修复**并各有守卫测试；5 项待验证风险已按"消除 / 已验证 / 接受"逐条处置。

| 类别 | 数量 | 状态 |
|---|---|---|
| P0 严重 | 0 | — |
| P1 高优先级 | 0 | — |
| P2 中优先级 | 5 | 已修复（含守卫测试） |
| P3 低优先级 | 6 | 已修复（6 项，含守卫测试或逐字节验证） |
| 待验证风险 | 5 | 消除 1、端到端验证 1、给出验证方法 3 |

## 2. P2：已修复

### P2-1 systemd 单元的行尾注释让加固指令被静默忽略

- **证据**：`systemd-analyze verify deploy/elenvind.service` → 6 条 `ignoring / Invalid argument`（`MemoryDenyWriteExecute`、`ReadWritePaths` 被行尾 `#` 之后的中文并进参数值）；把注释移到独立行后 **0 条**。
- **修复**：`deploy/elenvind.service` 所有注释独占一行，并在文件头写明这条 systemd 规则。
- **守卫**：`tests/test_operations.py::DeploymentFiles::test_systemd_unit_has_no_trailing_comments`（静态）+ `test_systemd_analyze_verify_reports_no_ignored_directives`（外部工具可用时）。
- **回归**：`systemd-analyze verify` 输出为空。

### P2-2 `ReadWritePaths` 覆盖整棵树（服务账号可改代码/模板/配置）

- **证据**：verify 显示 `/srv/elenvind` 被当作可写路径；`ProtectSystem=strict` 下这是唯一的可写面。
- **修复**：改为 `ReadWritePaths=-/srv/elenvind/data -/srv/elenvind/logs -/srv/elenvind/backups`（`-` = 目录缺失时不拒绝启动）；`config.example.toml` 与 `config.toml` 的 `database` 改为 `data/sqlite.db`，现有 `sqlite.db` 已移入 `data/`；README 给出 `root:elenvind` + `install -d` 的准备命令。
- **守卫**：`test_read_write_paths_are_narrow`、`test_shipped_default_database_lives_in_the_writable_subdirectory`。
- **验收**：`sudo -u elenvind test -w /srv/elenvind/elenvind/app.py` 必须失败。

### P2-3 带会话的每个请求都进入全局写事务（吞吐减半）

- **证据（修复前）**：匿名静态 1961 rps → 带会话 1015 rps（52%）。
- **修复**：`db/session.py:get_session_user()` 改为**读优先** —— 用只读连接判定；只有"刷新 `last_seen`"或"删除过期行"才进入 `write_tx()`（新增 `touch_session()`）。
- **守卫**：`tests/test_auth_lockout.py::test_session_lookup_does_not_take_the_write_lock`（把 `db.session.write_tx` 换成炸弹，读取仍必须成功）、`test_expired_session_is_deleted`。
- **实测（修复后）**：匿名 1939 rps、带会话 2182 rps（比值 **113%**）。

### P2-4 被限流的公开注册仍每请求取全局写锁

- **证据（修复前）**：被限流的 `POST /register` 803 rps / p50 7.1ms（匿名静态 1961 rps / 3.4ms）。
- **修复**：`db/auth.py:register_attempt_exceeded()` 只读预检；`modules/auth/routes.py:_try_register()` 先预检、超限直接返回，未超限才进 `try_register_attempt()` 的原子事务（权威判定不变）。
- **守卫**：`tests/test_operations.py::RegisterPreCheck`（把 `db.auth.write_tx` 换成炸弹，超限注册必须不触发；另一条确认未超限时仍能正常注册）。
- **说明**：8 线程注册洪水下评论延迟仍有退化，但那是 **2 个 sync worker 的排队争用**（洪水请求本身要渲染页面），不是写锁争用；对公开路径的边缘限流由 Nginx `limit_req` 负责（见 §5）。

### P2-5 登录闸门命中后仍执行 scrypt 且额外写一次库

- **证据**：闸门命中分支调用 `record_login_attempt()` → 每个公开可达的失败尝试 = 1 次 scrypt + 1 次 `write_tx`（上一轮"验证优先"修复引入）。
- **修复**：删除该分支的补记账。计数冻结无害：冷却到期后 `allowed=True` 的那次尝试会正常记账，冷却照旧逐次加深。
- **守卫**：`test_blocked_attempts_do_not_write_to_the_database`（闸门期内流水行数不变）+ `test_cooldown_expiry_records_and_escalates`（冷窗过后仍会记账）。
- **实测**：连续失败把流水推到 5 行后，闸门期内的 3 次失败使行数保持 5；正确密码随后仍 302 且流水清零。

## 3. P3：已修复或已缓解

| 编号 | 问题 | 处置 | 守卫 |
|---|---|---|---|
| P3-1 | 被限流的评论提交渲染整页（429 体积 = 文章页 102%） | 限流类 outcome 改为渲染轻量错误页（新 i18n `comment_err_rate_title`） | `test_operations.py::RateLimitResponseCost`（429 体积 < 文章页 50% 且不含评论正文） |
| P3-2 | 备份文件权限受 umask 影响（027 → 0640） | `db/backup.py` 成功后显式 `chmod 0600` | `test_operations.py::Backup::test_backup_is_consistent_and_private` |
| P3-3 | `.gitignore` 未忽略 `backups/`、`data/` | 已补两条 | `git check-ignore -v data/sqlite.db backups/x.db` |
| P3-4 | 并发登录可能留下 2 个会话 | `db/session.py:rotate_session()`：作废旧会话 + 颁发新会话在**一个写事务**；`core.session.rotate_session()` 改调它 | `test_concurrency.py::SessionRotation`（4 线程并发登录 → 恰好 1 行会话） |
| P3-5 | 改密 / 删号是多事务，中途失败留下不一致 | `db/user.py` 新增 `change_password_and_logout_all()`、`delete_user_account()`（各自单事务，含清流水与清会话）；`core.session.invalidate_user_sessions()` 因无消费者而删除 | `test_auth_lockout.py`（改密后旧会话失效）、`test_comments.py`/users 相关回归 |
| P3-6 | 备份失败抛 sqlite3 traceback；文档/测试一致性 | `db/backup.py` 把 `sqlite3.Error` 统一翻译成 `BackupError`；`test_doc_consistency` 在 `config.toml` 缺失时跳过；`AUDIT.md` 标注为历史归档并指向本文件 | `test_operations.py::Backup`（不可写目标 → 友好错误、无半成品） |

## 4. 待验证风险的处置

| 编号 | 风险 | 处置 |
|---|---|---|
| R-1 | 百分号编码的协议混淆（`java%09script:`） | **已消除**：净化层新增 `_ENCODED_CONTROL_RE`，拒绝 `%00`–`%1f`、`%7f`（`%20` / `%2F` 等正常编码不受影响）。守卫见 `test_core_contract.py::test_percent_encoded_control_characters_are_rejected` |
| R-2 | ENOSPC / 磁盘满行为 | **部分验证**：用 `ulimit -f` 制造写入失败 → 退出码 1、无 traceback、**无半成品残留**；只读挂载路径此前已验证（写 500 / 读 200 / 权限一致后自愈）。真实 ENOSPC 需在小容量挂载上演练（命令见 §6） |
| R-3 | 备份中断的失败语义 | **已验证**：目标不可写、写入中途 I/O 失败两种路径都是"一行原因 + 退出码 1 + 删除半成品"；仅 `SIGKILL` 这类不可捕获中断才会留下不完整文件（此时下次备份会**拒绝覆盖**它，属保守行为，已在 README 说明） |
| R-4 | Nginx 限流是否真的生效 | **已端到端验证**：用户态 Nginx（`listen 127.0.0.1:8080`）代理到应用，12 次连续 `/login` → `{200: 6, 503: 6}`，Nginx 日志 6 条 `limiting requests`；同时验证响应头 `Server: nginx`（`proxy_hide_header Server` 生效）、CSP 等安全头透传、`/healthz` 透传 `robots.txt`、`X-Forwarded-For` 传递 |
| R-5 | 锁定提示区分 email/ip/global 维度 | **接受**（不泄露账号是否存在；仅可推断"该邮箱近期被尝试过"） |

## 5. 已确认有效的防护（本轮复核，未改动）

- 并发评论不超发（6 并发 → 1 成功 / 5 个 429，评论行数 1，事务内判定）。
- 并发注册同邮箱只成功一次（6 并发 → 1 个 302，`user` 行数 1）。
- XSS：45 条 + 26 条混淆载荷（实体编码、控制字符、属性注入、畸形/未闭合标签、注释、代码块、表格、脚注、`@video`）在**解析器视角**下零危险标签、零事件属性、零危险协议；`_safe_url` 对 TAB/LF/CR 直接拒绝（与 Markdown 归一化构成双保险）。
- 评论是纯文本（不解析 Markdown）→ 公开输入无法触发渲染链。
- 迁移残留（`*_legacy`）→ 退出码 1 + 明确中文原因，不进入半迁移；`init_db()` 幂等。
- 只读数据库 → 写 500、读 200、权限恢复一致后自愈（无状态污染）。
- 日志轮转（`mv app.log app.log.1`）后 `WatchedFileHandler` 自动重建并继续写。
- 并发写入（60 条评论）期间的在线备份：`integrity_check=ok`、`foreign_key_check` 空、快照行数与时刻一致。
- 恢复演练：备份 0600 → 移走 `sqlite.db*` → 放回备份 → 启动 `200`、评论数 = 快照、启动日志 0 错误、恢复后写路径可用。
- 注册限流**确实**省掉 scrypt：旧实现下被限流的注册请求已跑到 803 rps（证明没有执行 240ms 哈希），本轮又加了只读预检，连全局写锁都不再获取（机械证明见 `RegisterPreCheck`）。
- 文件权限受 umask 控制（027 → `640`），锁文件恒 `600`；`db/backup.py` 现在固定 `600`。
- 日志不含密码 / 令牌 / 查询串；XFF / XFP 仅在受信对端生效；未知权限 fail-closed。

## 6. 发布前验收清单

```bash
# 1) 代码与守卫
.venv/bin/python -m unittest discover -s tests -t . 2>&1 | grep -E "^(Ran |OK|FAILED)"
.venv/bin/ruff check .

# 2) 部署文件
systemd-analyze verify deploy/elenvind.service          # 期望无输出
sudo systemctl show elenvind -p ReadWritePaths           # 期望仅 data/logs/backups
sudo -u elenvind test -w /srv/elenvind/elenvind/app.py && echo "★ 代码可写" || echo "OK"
nginx -t

# 3) 数据目录与权限
ls -l /srv/elenvind/data/                                # sqlite.db / -wal / -shm 权限一致
stat -c '%a %n' /srv/elenvind/data/sqlite.db /srv/elenvind/logs/app.log

# 4) 备份 / 恢复演练（每季度）
.venv/bin/python run.py --backup-to /tmp/verify.db && stat -c '%a' /tmp/verify.db   # 期望 600
sqlite3 /tmp/verify.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
.venv/bin/python run.py --backup-to /tmp/verify.db ; echo "期望非零退出（拒绝覆盖）"
# 恢复：停服 → mv data/sqlite.db* /tmp/broken/ → cp 备份 data/sqlite.db → 启动 → 首页/登录/评论

# 5) 异常路径
chmod 444 data/sqlite.db data/sqlite.db-wal data/sqlite.db-shm   # 写应 500、读应 200
chmod 644 data/sqlite.db data/sqlite.db-wal data/sqlite.db-shm   # 自愈
mv logs/app.log logs/app.log.1 && curl -s -o /dev/null https://your.domain/ && wc -l logs/app.log

# 6) 边缘限流（目标机）
ab -n 60 -c 10 https://your.domain/login | tail -5      # 期望出现 503（limit_req 生效）

# 7) 真实 ENOSPC 演练（可选，需要 root）
#    mount -t tmpfs -o size=4M tmpfs /mnt/tiny && 把 database 指到 /mnt/tiny 后压评论
```

## 7. 仍可记录后接受的事项

1. **登录闸门命中后仍会执行一次 scrypt**（≈94 ms/次，2 worker 下约 10 次/秒，实测）：这是"验证优先"的必然代价 —— 闸门必须先校验密码才能判断"这次到底该不该拦"。它是**刻意**保留的取舍，因为替代方案（闸门命中即拒绝）会让正确密码也被锁在门外。缓解：Nginx `/login` 边缘限流（示例 `20r/m`，实测 12 次请求触发 6 个 503）+ 邮箱/IP/全站三闸门只降级不阻断 + 失败尝试不再额外写库（P2-5）。压力评估方法：`ab -n 200 -c 20 https://your.domain/login` 观察 CPU 与响应时间。
2. **2 个 sync worker 的排队争用**：8 线程洪水下正常写请求 p50 会升高（实测 2.4ms → 10.7ms）。这是 Gunicorn 同步 worker 的容量事实，不是逻辑缺陷；缓解手段是 Nginx 边缘限流（已配）与按机器调 `--workers`。
3. **`SIGKILL` 中断备份会留下不完整目标文件**：下一次备份会拒绝覆盖它（保守设计）。运维只需删除或改名；如需自动化，可在 cron 里先清理超过 N 天的 `.db`。
4. **锁定提示区分维度**：可推断"该邮箱近期被尝试过"，接受。
5. **日志随请求量线性增长**（约 135 B/行）：`[logging].rotate = false` + `deploy/logrotate.conf` 已经处理；静态资源在 Nginx 侧已 `access_log off`。
6. **真实 ENOSPC 与真实 systemd 运行环境**未在本机演练（无 root / 无目标目录），命令已列在 §6。

## 8. 本轮改动清单（文件 → 目的）

```
elenvind/core/session.py        rotate_session 改为调用 store.rotate_session；删除无消费者的 invalidate_user_sessions
elenvind/core/markdown.py       拒绝百分号编码的控制字符（%00–%1f、%7f）
elenvind/core/version.py        v1.0.0（stage 为空不再拼 "-"）
elenvind/db/session.py          get_session_user 读优先（只读连接）+ touch_session；rotate_session 单事务
elenvind/db/user.py             change_password_and_logout_all / delete_user_account（各自单事务）
elenvind/db/auth.py             register_attempt_exceeded 只读预检；删除 clear_login_attempts（已被合并事务取代）
elenvind/db/backup.py           sqlite3.Error → BackupError 统一翻译；备份固定 0600
elenvind/db/__init__.py         导出面同步
elenvind/modules/auth/routes.py 删除闸门期补记账；注册先做只读预检
elenvind/modules/users/routes.py 改密/删号改用事务合并版本 + 清浏览器 Cookie
elenvind/modules/blog/routes.py 限流响应改为轻量错误页
i18n/{en,zh,ja}.toml            新增 comment_err_rate_title（三语一致）
deploy/elenvind.service         行尾注释修正 + ReadWritePaths 收窄 + 数据目录说明
deploy/nginx.conf.example       server 级公共代理参数、proxy_read_timeout 40s、静态资源 access_log off、zone 说明
config.toml / config.example.toml  database = data/sqlite.db（现有库已迁入 data/）
.gitignore                      data/ 与 backups/
README.md / docs/CONFIGURATION.md  目录布局、迁移、备份/恢复、排障、测试矩阵
tests/test_concurrency.py       新增：真正并发的三条回归
tests/test_operations.py        新增：备份/限流成本/预检/迁移/部署文件校验
tests/test_auth_lockout.py      闸门不写库、冷却到期记账、会话读取不占写锁、过期删除
tests/test_core_contract.py     编码控制字符断言
tests/test_doc_consistency.py   config.toml 缺失时跳过 + 两份配置键集合一致
```
