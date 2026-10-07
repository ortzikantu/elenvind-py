# 数据库与 C0 写协调

> **适用读者**：要写数据库代码、排查写竞争/慢事务、或做备份与恢复的人。
> **一句话**：读走 `connect()`，**所有写**走 `write_tx()` —— 它在独立锁文件上取
> `flock(LOCK_EX)`，再 `BEGIN IMMEDIATE`，事务结束才释放锁。

---

## 1. 两条入口

| 入口 | 用途 | 语义 |
|---|---|---|
| `with connect() as conn:` | **只读**（SELECT / PRAGMA 查询） | 打开连接 → 用事务包住（SQLite 语义：正常提交、异常回滚）→ 关闭。不参与 flock |
| `with write_tx() as conn:` | **唯一合法写入口**（INSERT / UPDATE / DELETE / DDL） | 跨进程排他锁 → 连接 → `BEGIN IMMEDIATE` → 业务 SQL → 提交/回滚 → 关连接 → 解锁 |

```python
from elenvind.core.db_base import connect, write_tx

with connect() as conn:                      # 读
    row = conn.execute("SELECT ... WHERE id = ?", (user_id,)).fetchone()

with write_tx() as conn:                     # 写
    conn.execute("UPDATE comment SET is_deleted = 1 WHERE id = ?", (comment_id,))
```

**模块不得**：`import sqlite3`、`get_connection()`、自己 `BEGIN`/`COMMIT`、把写操作放进
`connect()`。这些由 `tests/test_core_contract.py` 的 AST 守卫拦截（模块里连
`.execute(` 都不允许出现）。

---

## 2. `write_tx()` 的生命周期

```python
def write_tx(*, foreign_keys=True, ensure_wal=False):
    # 1. 拒绝嵌套（同线程再进一次会自死锁）
    # 2. flock(LOCK_EX) on lock_path_for(DB_PATH)      ← 跨进程互斥，阻塞等待
    # 3. get_connection()：PRAGMA foreign_keys / busy_timeout [/ journal_mode=WAL]
    # 4. BEGIN IMMEDIATE                                ← 立刻拿 SQLite 写锁
    # 5. yield conn                                     ← 业务 SQL
    # 6. commit()   或（异常时）rollback() 后原样抛出
    # 7. close()                                        ← finally，路径全覆盖
    # 8. flock(LOCK_UN) + close(fd)                     ← finally，路径全覆盖
```

顺序就是保证本身：**锁在 `BEGIN IMMEDIATE` 之前拿到**（不会"两个进程各拿 SQLite
写锁再互相等"），**锁覆盖整个事务窗口**（不是只锁住 BEGIN 那一瞬间）。

| 情况 | 行为 |
|---|---|
| 正常退出 | `COMMIT`；提交失败会抛异常（**绝不谎报成功**）并尽力 `ROLLBACK` |
| 业务抛 `Exception` | `ROLLBACK` → 原样重抛（`IntegrityError` 等留给调用方判定） |
| `BaseException`（`KeyboardInterrupt` / `SystemExit`） | 同样回滚再抛，不留下未提交数据 |
| `BEGIN IMMEDIATE` 就失败（如 `busy_timeout` 到点） | 连接与锁照样释放（不泄漏 fd） |
| 进程被 `SIGKILL` | 内核关闭 fd 自动释放 flock；未提交事务由 SQLite 回滚（WAL 保证） |
| 嵌套调用（同一线程） | 立刻 `RuntimeError`（说明写错了结构，而不是挂死） |

**禁止嵌套**：`flock` 在**新的 fd** 上会自死锁，所以"复合写操作里又调用了一个写函数"
被显式做成异常。正确写法是把它们放进**同一个** `with write_tx()` 块，或在块外顺序调用
（例如 `db_prune.prune()` 自己就是一个写事务，必须在其它 `write_tx()` **之外**调用）。

---

## 3. 锁文件规则

```
sqlite.db                  ← 数据库
sqlite.db.write.lock       ← 锁文件（与数据库同目录、路径由数据库路径派生）
```

| 规则 | 原因 |
|---|---|
| **独立于数据库文件** | 不拿数据库本身当锁；避免平台/版本差异与"锁文件被 SQLite 接管"的混淆 |
| **由当前数据库路径稳定派生**（`lock_path_for()` 每次重新计算，不缓存） | `ELENVIND_DB` / `[database]` 切换后，锁文件跟着换，**不会锁错库** |
| **永不删除**（代码里没有任何 `unlink`） | 锁的语义绑定 inode；删除重建会让不同进程锁住不同 inode，互斥直接失效 |
| 权限 `0600`、`O_CLOEXEC` | 不给其它用户可写；`exec` 时不把锁 fd 泄漏给子进程 |
| 多个 worker 得到**同一路径** | 路径只由 DB 路径决定，与进程无关 → 跨进程互斥成立 |
| 不同数据库得到**不同锁** | 两个库各自独立，互不阻塞（有测试） |

**运维注意**：备份/迁移数据库时，锁文件可以留着（下次启动复用）；**不要**在服务运行时
删除锁文件。若确实要重建（例如换了目录），先停服务。

---

## 4. 这个模型保证什么、不保证什么

**保证**（测试覆盖）：

- 所有遵守 `write_tx()` 协议的写入者，在**单机多进程**环境下不会同时进入 SQLite
  写事务；写事务不会相互交错（临界区实测不重叠）；
- 读不受写锁阻塞：写事务进行中，`connect()` 的 SELECT 仍然可用（WAL）；
- 异常、键盘中断、`SIGKILL` 都不会留下跨进程锁；未提交的写入不会"半落地"；
- 并发的 `init_db()` / 迁移会串行执行，schema 只会到达一个最终版本。

**不保证**（不要对外声称）：

- **不是严格 FIFO**：`flock` 只保证互斥，等待的进程谁先拿到锁不做承诺；
- **不承诺"绝对不会损坏"**：它保证的是"遵守协议的写入者不会并发写"；协议之外的写入者
  （例如有人拿 `sqlite3` 命令行直接写）不受约束，SQLite 自身的崩溃一致性才是最后防线；
- **不跨机器**：`flock` 是本地文件系统语义；不要放在 NFS/共享存储上跑多机写入；
- **不解决慢**：写是串行的，加 worker 只对读有帮助。写得慢要优化 SQL/事务长度，而不是加队列。

刻意**没有**引入：应用层队列、writer 进程/线程、重试框架、锁管理器、
锁超时配置、事务管理器框架。`flock` 阻塞等待就是全部机制。

---

## 5. 读 → 判断 → 写（必须原子时）

**反例（错误）**：

```python
with connect() as conn:                  # 读
    row = conn.execute("SELECT ...").fetchone()
if row:                                  # 判断（此刻可能已经变了）
    with write_tx() as conn:             # 写
        ...
```

如果业务语义要求"读到的事实"与"写下的动作"一致，就要把**读也放进写事务**：

```python
with write_tx() as conn:                 # 同一个 BEGIN IMMEDIATE 内读+写
    row = conn.execute("SELECT ...").fetchone()
    if row is None:
        conn.rollback()                  # 判定后放弃：不落任何数据
        return "empty"
    conn.execute("UPDATE ...")
```

本项目的现状（哪些地方刻意放在事务内、哪些刻意放在事务外）：

| 场景 | 处理 | 理由 |
|---|---|---|
| 会话读取并刷新 `last_seen`（`get_session_user`） | **整段在一个 `write_tx()` 内** | 读-判断-写复合：过期就删、否则刷新，两件事必须一致 |
| 评论限流 + 插入（`try_post_comment`） | **一个事务**（计数与插入同锁） | 限流必须与写入原子，否则并发下能超额 |
| 评论父级存在性 / 跨文章 / 深度上限 | 事务内**权威**判定（路由里的预检只是 UX） | 预检在事务外属 TOCTOU，不能作为判据 |
| 注册撞邮箱 | `create_user()` 里 `INSERT` 抛 `IntegrityError`；调用方据此提示 | UNIQUE 约束是最终权威，不做"先 SELECT 再 INSERT" |
| 登录失败计数（`record_login_attempt`） | 独立写事务 | 计数是辅助防线，不要求与其它写入原子 |
| 文章索引/正文缓存 | 进程内内存 + 文件状态快照 | 与数据库无关 |

---

## 6. 建表、迁移与 `init_db()`

`init_db()` 的全部内容在**一个** `write_tx(foreign_keys=False, ensure_wal=True)` 里：

```
flock
 ├─ 残留检查：上次迁移留下的 *_legacy 表存在 → 拒绝启动（MigrationError，人工介入）
 ├─ _create_schema()：CREATE TABLE IF NOT EXISTS ...（新库一次建好）
 ├─ _run_migrations()：按 PRAGMA user_version 逐级迁移（v1 → v4）
 │     · 重建表：ALTER TABLE x RENAME TO x_legacy → CREATE → INSERT ... SELECT →
 │       DROP TABLE x_legacy → 重建索引
 │     · 数据规范化：如 v4 把邮箱统一小写
 │     · 每级成功后写 PRAGMA user_version = N
 └─ COMMIT / 异常整体 ROLLBACK
unlock
```

要点：

- **迁移是事务性的**：中途失败整体回滚，schema 与 `user_version` 不会停在半成品；
- **DDL 不再是隐式提交**：`CREATE TABLE` 在 `BEGIN IMMEDIATE` 之后执行，失败会一起回滚
  （历史实现用普通连接跑 DDL，`CREATE TABLE` 立即提交，会留下"建了一半的表"）；
- **多 worker 同时启动是安全的**：flock 让它们串行；第一个建表迁移，其余发现版本已最新，
  什么都不做。有 4 进程并发初始化 / 非幂等迁移只执行一次 / legacy 库保留数据 的测试；
- **残留表不会被静默忽略**：发现 `*_legacy` 直接拒绝启动，要求人工恢复（这是刻意的：
  自动"清理"会掩盖一次失败的迁移）；
- `foreign_keys=False` 仅用于重建表；`ensure_wal=True` 仅用于初始化（WAL 是持久化的
  文件属性，普通连接不再切换 journal mode）。

---

## 7. 连接设置

| 设置 | 值 | 说明 |
|---|---|---|
| `PRAGMA foreign_keys` | `ON`（迁移时 `OFF`） | **每连接**开关，所以 `_configure_connection()` 每次显式设置 |
| `PRAGMA busy_timeout` | 5000 ms | 协议外写入者/极端争用时，SQLite 层再兜一层等待 |
| `PRAGMA journal_mode` | `WAL` | 由 `init_db()` 确立一次；读不阻塞写、写不阻塞读 |
| `row_factory` | `sqlite3.Row` | 行支持按列名取值（`row["nickname"]`；不支持 `getattr`） |

PRAGMA 必须在 `BEGIN` **之前**执行 —— `journal_mode` 与 `foreign_keys` 在事务内是空操作，
所以它们集中在 `_configure_connection()` 里。

---

## 8. 可观测性

| 日志 | 级别 | 触发 |
|---|---|---|
| `Write lock acquired in X ms (path=…)` | DEBUG | 正常拿到锁（无竞争） |
| `Write lock wait N ms (path=…)` | **WARNING** | 等锁 ≥ `_SLOW_LOCK_WAIT_SECONDS`（默认 1s）→ 写竞争已经可观 |
| `Write transaction committed: X ms, N row(s) changed (path=…)` | DEBUG | 每次提交（`[logging].level = "debug"` 才可见） |
| `Write transaction slow: X ms, N row(s) changed (path=…)` | **WARNING** | 事务 ≥ `_SLOW_WRITE_SECONDS`（默认 1s） |
| `Write transaction rolled back after X ms (path=…)` | DEBUG | 异常/主动放弃路径 |
| 迁移相关（每级、规范化行数、残留表） | INFO / WARNING | 启动时 |

排障配方（`logs/app.log`）：

```bash
grep "Write lock wait" logs/app.log | tail          # 写竞争是否成为常态
grep "Write transaction slow" logs/app.log | tail   # 哪些写变慢了（对照 SQL 改）
grep "Database:" logs/app.log | tail -3             # 库路径 / schema 版本 / journal_mode / 锁文件
```

---

## 9. 数据访问 API（Core 提供的业务 DB 层）

| 模块 | 主要函数 |
|---|---|
| `db_user` | `create_user`、`get_user_by_email`、`get_user_by_id`、`update_user_profile`（原子改昵称+邮箱）、`update_user_password`、`delete_user`（逻辑删除）、`get_user_number` |
| `db_session` | `create_session`、`get_session_user`（读+刷新，单事务）、`delete_session`、`delete_user_sessions`、`cleanup_expired_sessions` |
| `db_login` | `record_login_attempt`、`count_email_failures`、`count_ip_failures`、`count_global_recent_failures`、`clear_login_attempts`、`cleanup_old_login_attempts` |
| `db_register` | `try_register_attempt`（原子限流）、`cleanup_old_attempts` |
| `db_comment` | `get_comments_by_article`、`get_comment_by_id`、`create_comment`、`soft_delete_comment`、`restore_comment`、`comment_depth`、`flatten_comment_tree` |
| `db_comment_rate` | `try_post_comment`（限流 + 深度 + 插入，同一事务）、`cleanup_old_comment_attempts` |
| `db_prune` | `prune`（机会式清理，**独立写事务**）、`due`、`reset_state` |

**加一个新的写操作**：在对应 `db_*.py` 里写一个函数，函数体只用
`with write_tx() as conn:`；不要新增连接管理、不要新增事务包装层。若它属于"读+判断+写"
（见 §5），整段放进同一个事务。

---

## 10. 备份、恢复与巡检

```bash
# 备份（WAL 模式下**不要**裸拷贝文件）
sqlite3 sqlite.db ".backup '/var/backups/elenvind-$(date +%F).db'"

# 巡检
sqlite3 sqlite.db "PRAGMA integrity_check;"
sqlite3 sqlite.db "PRAGMA user_version;"      # 期望 4（SCHEMA_VERSION）
sqlite3 sqlite.db "SELECT name FROM sqlite_master WHERE type='table';"
```

- 期望的表：`user`、`session`、`login_attempts`、`register_attempts`、`comment`、
  `comment_rate`；出现 `*_legacy` 说明上次迁移未完成 → 服务会拒绝启动，按提示恢复；
- 锁文件 `sqlite.db.write.lock`（0 字节）是**正常存在**的，不是残留垃圾；
- 恢复：停服务 → 放回数据库文件 → 启动（`init_db()` 会补齐缺失表并按 `user_version` 迁移）。

更多运维操作（解锁限流、清理过期会话等）见 [../OPS_GUIDE.md](../OPS_GUIDE.md)。

---

## 11. 测试

```bash
python -m unittest tests.test_c0_write_tx -v      # 单进程语义：提交/回滚/嵌套/锁持有/设置
python -m unittest tests.test_c0_concurrency -v   # 真实多进程：并发写、临界区不重叠、SIGKILL、并发迁移
python -m unittest tests.test_core_contract -v    # 静态：模块不得绕开 write_tx()
```

覆盖点：锁文件独立/稳定/跟随 DB 路径、不同库不同锁、读不被写锁阻塞、锁覆盖整个事务
（含回滚路径）、`BEGIN IMMEDIATE` 语义保留、COMMIT 失败不谎报成功、BEGIN 失败不泄漏 fd、
4 进程并发写不丢数据、`SIGKILL` 后自动恢复、非幂等迁移只执行一次。细节见
[testing.md](testing.md)。
