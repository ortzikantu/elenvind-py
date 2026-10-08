# 贡献指南

> **适用读者**：要给这个项目提交补丁的人。
> **一句话**：补丁形式提交（`git format-patch`），单一主题，带测试，不放宽守卫。

---

## 1. 先读什么

| 顺序 | 文档 | 为什么 |
|---|---|---|
| 1 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层、依赖方向、组合入口、关键不变量 |
| 2 | [docs/development/modules.md](docs/development/modules.md) | 业务代码怎么写、什么不许写 |
| 3 | [docs/development/database.md](docs/development/database.md) | 写数据库的唯一方式（`write_tx()`） |
| 4 | [docs/development/testing.md](docs/development/testing.md) | 测什么、怎么跑、守卫拦什么 |

环境：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml     # 改 site_url / title / [static]
```

---

## 2. 提交方式

**不接受 Pull Request，只接受补丁**（作者本人维护，避免仓库权限与 review 噪音）：

```bash
git format-patch -1            # 最新一次提交
git format-patch origin/main   # 尚未推送的全部提交
```

把 `.patch` 邮件发过来。要求：

- **一次补丁一个主题**：修 bug 就只修 bug，不要顺手重构；
- **说明"为什么"**：提交信息写清动机与影响面（"修了什么现象、为什么这样修"），
  不要只写"update"；
- **自测通过**：至少跑过受影响的模块 + 三个守卫（见 §4）。

---

## 3. 代码风格

| 规则 | 说明 |
|---|---|
| 同步 | 业务代码是普通 `def`。**不要**引入 `async`/`await`/asyncio（有守卫测试） |
| 标准库优先 | 新增运行期依赖要先论证；`requirements.txt` 只放真正需要的 |
| KISS | 能用一个函数解决的不要引入类；能显式传参的不要引入注册表/容器 |
| 分层 | Core 不认识 db/模块；db 不认识 core/模块；模块之间零 import；跨模块协作由 `elenvind/app.py` 注入 |
| SQLite | 只有 `elenvind/db/` 可以 `import sqlite3`、执行 SQL、提交事务（有 AST 守卫） |
| 写库 | 只能 `with db.write_tx() as conn:`；读用 `with db.connect() as conn:` |
| 安全 | CSRF / Cookie / 密码 / 安全头 / 请求限制由 Core 提供，模块**不要**重复实现 |
| 注释 | 写"为什么这样做"与"边界在哪里"，而不是复述代码；中文 |
| 文案 | 面向用户的文案放 `i18n/*.toml`（zh/en/ja 三份保持同步） |

**明确不要**（会被架构守卫或评审拒绝）：ORM、DI 容器、Service Locator、
Event Bus、Repository / Unit of Work、应用层队列 / writer 进程 / 重试框架、
插件系统、把 Core 再切成更多架构层。

---

## 4. 提交前必须跑的

```bash
python -m unittest tests.test_architecture -v      # 分层与依赖方向
python -m unittest tests.test_core_contract -v     # 安全契约（模块不得绕过 Core）
python -m unittest tests.test_doc_consistency -v   # 文档与代码一致
python -m unittest discover -s tests -t .          # 全量（约 140 s）
python smoke_driver.py                             # 端到端（真实 Gunicorn，61 项）
```

改动涉及写事务/迁移时，额外跑：

```bash
python -m unittest tests.test_c0_write_tx tests.test_c0_concurrency -v
```

---

## 5. 加功能的检查清单

- [ ] 新能力放进合适的模块（只服务一个业务的规则不要塞进 Core）
- [ ] 路由用声明式授权（`auth=` / `permission=`），不要自己写认证判断
- [ ] 写操作走 `write_tx()`；"读-判断-写"要原子时把读放进同一个事务
- [ ] 用户可见文案进 `i18n/*.toml`（三语同步）
- [ ] 新模块登记到 `tests/test_architecture.py::MODULE_ENTRYPOINTS` 与文档
- [ ] 带测试：正常路径 + 至少一个边界/失败路径
- [ ] 新文档/改文档：同步 [docs/README.md](docs/README.md) 索引（守卫会检查）
- [ ] 改配置键：同步 `config.example.toml` 与 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- [ ] 改 `write_tx()` 语义/阈值：同步 [docs/development/database.md](docs/development/database.md)

---

## 6. 安全漏洞

**不要开公开 issue**。走私有渠道报告，附最小复现（请求 + 期望/实际）与影响评估；
修复以补丁形式给出。已知的取舍与威胁模型见 [docs/SECURITY.md](docs/SECURITY.md)，
先看看是否属于"已文档化的刻意设计"。
