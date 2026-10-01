# 站点部署指南（生产环境）

Elenvind 是"应用服务器 + 独立静态托管"两段式架构：

- **应用**：uvicorn 跑 Python SSR（账号/评论/文章渲染/SEO 文件），只监听本机回环；
- **静态**：CSS、图标、图片由 Nginx（或 CDN）直接服务，应用不参与。

推荐拓扑：

```
浏览器 ──HTTPS──> Nginx(80/443)
                    ├── /static、/assets、*.ico、*.png … → 磁盘静态文件
                    └── 其余全部路径 ──反代──> 127.0.0.1:6789 (uvicorn/Elenvind)
```

---

## 一、环境要求

| 项目 | 要求 |
|---|---|
| Python | **3.11+**（配置解析使用标准库 `tomllib`） |
| 依赖 | 仅 `uvicorn`（`requirements.txt` 另含其间接依赖 click/h11） |
| 数据库 | 无需安装——SQLite，首次启动自动建表（WAL 模式） |
| 反向代理 | Nginx（或等效），负责 HTTPS 与静态资源 |

## 二、安装与首次启动

```bash
git clone https://codeberg.org/ortzikantu/elenvind.git elenvind-py
cd elenvind-py

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 直接前台试跑（开发验证）——不需要任何环境变量
python run.py
```

浏览器访问 `http://127.0.0.1:6789/`。`logs/app.log` 会记录启动过程与后续全部日志。

可选环境变量：`ELENVIND_DB` —— 覆盖数据库文件路径（默认项目根 `sqlite.db`），
多环境隔离或自动化测试时使用，生产一般不需要。

## 三、生产部署（推荐单进程 + systemd + Nginx）

### 1. 修改配置文件

编辑 `config.toml`（模板见 `config.example.toml`）：

```toml
site_url = "https://example.com"   # robots.txt / sitemap.xml 的绝对地址来源（必改）

[server]
    host = "127.0.0.1"     # 只允许本机回源，配合 trusted_proxies 安全边界
    port = 6789
```

同步按需填写 `[static]` 与 `params.social.icon` 等 URL（指向 Nginx 静态目录）。
配置在启动时校验，取值非法会直接拒绝启动并在控制台/日志给出具体原因。

### 2. 准备静态资源目录

**样式表是可选的**：`[static].css` 留空时，应用自己会发出内置样式表
（`/css/style.css`），因此"只跑 `python run.py`"也有完整排版。

**仓库不附带图片资产**（favicon / logo / 社交图标 / hero 图）。这些 URL 留空时
对应元素**不渲染**，所以开箱不会有裂图或 404；要显示它们就把自己的文件放到
Nginx 站点根目录，再在 `[static]` 与 `params.social.icon` 里填 URL
（目录结构见 `docs/nginx.conf.example` 头部注释）。

想把 CSS 也交给 Nginx 托管（更省应用进程、便于 CDN 缓存），就把仓库根的
`style.example.css`（内置样式表的副本）作为起点改好，同步到 Nginx 站点根目录，
再把 `[static].css` 指过去。
**改 CSS/图标后只需同步文件，应用无需重启**——应用侧的内置样式表同样是
按请求读盘 + `ETag`，改完刷新即生效。

### 3. systemd 常驻

`/etc/systemd/system/elenvind.service`：

```ini
[Unit]
Description=Elenvind Personal Website Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/elenvind-py
ExecStart=/opt/elenvind-py/.venv/bin/python run.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now elenvind
sudo systemctl status elenvind
```

> 为什么是单进程？站点规模下多 worker 只会平摊进程内缓存（文章索引/渲染缓存）
> 并增加 SQLite 写竞争。uvicorn 单 worker 已足够；如需扩容优先考虑读多写少的
> 静态层，而不是应用层。

### 4. Nginx 反代 + HTTPS

完整配置模板见 **`docs/nginx.conf.example`**（含 TLS、静态缓存、回源头、自检清单），
复制后替换域名与证书路径：

```bash
sudo cp docs/nginx.conf.example /etc/nginx/sites-available/elenvind
sudo ln -s /etc/nginx/sites-available/elenvind /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

证书（以 Let's Encrypt 为例）：

```bash
sudo certbot certonly --standalone -d example.com -d www.example.com
# 或 webroot 模式（配置里已预留 .well-known 路由）
sudo certbot certonly --webroot -w /var/www/elenvind -d example.com
```

### 5. 与代码安全边界的联动（务必核对）

| 应用侧设定 | Nginx 侧配合 |
|---|---|
| `trusted_proxies` 默认只信 `127.0.0.1` 的 `X-Forwarded-For` | 反代目标必须写 `http://127.0.0.1:6789` |
| `run.py` 已启用 `proxy_headers`（仅信 127.0.0.1） | 必须透传 `X-Forwarded-Proto $scheme`，否则 HTTPS 下 Secure Cookie 不生效 |
| 应用统一下发安全头（CSP 等） | **不要在 Nginx 重复添加 CSP**（多个 CSP 头取并集会误伤页面） |
| 请求体上限 1 MB | `client_max_body_size 1m` |

## 四、上线自检清单

```bash
nginx -t                                              # Nginx 语法
curl -I https://example.com/                          # 200
curl -I https://example.com/robots.txt                # cache-control: public, max-age=3600
curl -s https://example.com/sitemap.xml | head -3     # 合法 XML、含文章 URL
curl -I https://example.com/login                     # 无 Server 版本泄露
# 浏览器：登录一次 → 开发者工具确认 session Cookie 带 Secure + SameSite=Lax
# 手机/窄屏：首页、文章页、菜单换行正常
# 深色系统 + 手动切换：Dark/Light 各自生效且刷新后保持
```

## 五、版本升级

```bash
cd /opt/elenvind-py
git pull
.venv/bin/pip install -r requirements.txt      # 依赖有变化时
python -m unittest discover -s tests -t .      # 可选：先跑一遍自带测试
sudo systemctl restart elenvind
```

数据库迁移：schema 版本记录在 `PRAGMA user_version`，启动时会自动检测并执行迁移
（`db_base.migrate`，幂等、可重复执行），**旧库可以直接启动**，无需手工改表。
`articles/*.md` 与 `custom_pages/*.md` 为纯内容文件，升级不会触碰；
**升级前仍建议先做一次数据库备份**（见运维文档）。

## 六、备份（重要）

SQLite 处于 WAL 模式时**不要直接拷贝 `sqlite.db`**，用：

```bash
sqlite3 sqlite.db ".backup '/backup/elenvind-$(date +%F).db'"
```

建议 cron 每日执行；同时备份 `articles/` 与 `custom_pages/`（内容即文件，直接打包即可）。
