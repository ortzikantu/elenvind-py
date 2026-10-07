# Elenvind 文档索引

> **一句话架构**：Gunicorn / WSGI → Composition Root（`elenvind/app.py`）→ Modules → Core → SQLite；
> 所有业务写入统一经过 `write_tx()`（独立锁文件 + `flock(LOCK_EX)` + `BEGIN IMMEDIATE`）。

本目录是项目的完整手册。全部中文；代码、配置键与命令都经过测试守卫，不会与实现漂移
（见文末「文档与代码的一致性」）。

---

## 我该看哪一篇？

| 我想…… | 去看 |
|---|---|
| 把站点跑起来看看 | 根目录 [README](../README.md) 的 Quick start |
| 部署到服务器（systemd / Nginx / HTTPS / 备份） | [DEPLOYMENT.md](DEPLOYMENT.md) |
| 改配置（站点标题、限流、会话、安全头、日志…） | [CONFIGURATION.md](CONFIGURATION.md) |
| 搞懂代码是怎么分层的、为什么这么分 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 新增一个业务模块 / 页面 | [development/modules.md](development/modules.md) |
| 写数据库、理解写事务与锁 | [development/database.md](development/database.md) |
| 跑测试 / 加测试 / 理解守卫 | [development/testing.md](development/testing.md) |
| 日常运维：文章、评论、用户、日志、限流误锁、FAQ | [OPS_GUIDE.md](OPS_GUIDE.md) |
| 安全模型：信任边界、CSP、CSRF、会话、限流、日志红线 | [SECURITY.md](SECURITY.md) |
| 抄一份 Nginx 配置 | [nginx.conf.example](nginx.conf.example) |

**推荐阅读顺序**（第一次接触这个代码库）：
[ARCHITECTURE.md](ARCHITECTURE.md) → [development/modules.md](development/modules.md)
→ [development/database.md](development/database.md) → [development/testing.md](development/testing.md)
→ [SECURITY.md](SECURITY.md)。

---

## 类目总览

### 🚀 使用与部署

| 文档 | 内容 |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | 环境要求、安装、Gunicorn + systemd + Nginx + HTTPS、上线自检清单、版本升级、备份、**数据库与锁文件的部署要求** |
| [nginx.conf.example](nginx.conf.example) | 可直接使用的反代示例（含代理头正确写法与静态资源映射） |

### ⚙️ 配置

| 文档 | 内容 |
|---|---|
| [CONFIGURATION.md](CONFIGURATION.md) | `config.toml` 每一个键：默认值、语义、坑（顶层键 / `[server]` / 限流 / `[static]` / `[params]` / `[security]` / 会话 / `[logging]` / 启动校验） |

### 🧱 开发

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 分层与依赖方向、组合入口、请求流水线、模块清单与职责、写入路径、KISS 原则与"什么不进 Core" |
| [development/modules.md](development/modules.md) | 模块开发指南：五分钟写一个模块、路由声明即安全、允许/禁止清单、模板约定、内容格式、日志怎么记 |
| [development/database.md](development/database.md) | 数据库与 C0 写协调：`connect()` / `write_tx()` 契约、锁文件、保证与不保证、迁移与并发、可观测性、读-判断-写边界 |
| [development/testing.md](development/testing.md) | 测试体系：测试清单与用途、命令、架构守卫 / Core Contract、多进程 C0 测试、如何加测试 |

### 🛠️ 运维

| 文档 | 内容 |
|---|---|
| [OPS_GUIDE.md](OPS_GUIDE.md) | 启停与日志、内容热更新、用户与评论、数据库操作、限流误锁处理、主题、FAQ、日常安全检查清单 |

### 🔐 安全

| 文档 | 内容 |
|---|---|
| [SECURITY.md](SECURITY.md) | 威胁模型与信任边界、安全响应头、CSRF、会话与密码、限流、日志红线、部署侧加固与验证命令 |

### 🤝 参与开发

| 文档 | 内容 |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 补丁流程、代码风格、提交前检查清单、加功能的检查清单、漏洞报告渠道 |

---

## 文档与代码的一致性

文档里的**具体断言**（天数、键名、脚本路径、符号名）由测试锁住，改代码忘了改文档会变红：

| 守卫 | 覆盖 |
|---|---|
| `tests/test_doc_consistency.py` | 会话天数与代码常量一致；`max_comment_depth = 0` 不得被写成"不限层级"；`admin_user_id` 语义；引用的脚本/符号/静态资源真实存在；`config.toml` 与 `config.example.toml` 键集合不结构性缺漏；CSP 示例不得出现未加引号的 `self`；**本索引必须列出所有文档** |
| `tests/test_architecture.py` | README 指向 `docs/development/modules.md`；旧的 `features/` 路径不得残留 |

因此，改动以下内容时请同步更新对应文档：

| 改动 | 需要同步 |
|---|---|
| 新增/修改配置键 | [CONFIGURATION.md](CONFIGURATION.md) + `config.example.toml`（两份配置的键集合必须一致） |
| 会话过期默认值 | [CONFIGURATION.md](CONFIGURATION.md)、[OPS_GUIDE.md](OPS_GUIDE.md)（守卫会核对天数） |
| 新增/删除模块 | [ARCHITECTURE.md](ARCHITECTURE.md)、[development/modules.md](development/modules.md)、`tests/test_architecture.py` 的模块清单 |
| 改 `write_tx()` 语义或阈值 | [development/database.md](development/database.md)、[development/modules.md](development/modules.md#7-数据访问读-connect写-write_tx) |
| 新增测试套件 | [development/testing.md](development/testing.md)、本索引 |
| 新增文档 | 本索引（守卫会检查） |
| 部署/代理/安全头相关 | [DEPLOYMENT.md](DEPLOYMENT.md)、[SECURITY.md](SECURITY.md)、[nginx.conf.example](nginx.conf.example) |
