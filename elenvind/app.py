"""Elenvind 应用装配点（Composition Root）。

这里是**唯一**允许同时认识 `core`、`db` 与各业务模块的地方：

    elenvind/app.py      装配：db 路径与启动、路由注册顺序、错误页、
                         会话加载、模块间注入、启动钩子
    elenvind/wsgi.py     生产入口：startup(STARTUP_HOOKS, prepare_database) + application
    elenvind/core/       项目专属运行时基座（不认识 db，也不认识 modules）
    elenvind/db/         持久化基座（唯一 SQLite 边界；不认识 core/modules）
    elenvind/modules/    业务模块（可依赖 core 与 db；模块之间零 import）
    elenvind/templates/  Jinja2 模板
    elenvind/static/     静态资源

依赖方向（`tests/test_architecture.py` 守卫）：

    Application（本文件 / wsgi.py）
        │
        ├──► core       （core ↛ db、core ↛ modules）
        ├──► db         （db ↛ core、db ↛ modules）
        └──► modules    （modules ↛ modules、modules ↛ app）

装配顺序 = 优先级（与历史行为一致）：

1. 静态资源（`/<path:relative>`，fallback）；
2. auth / users / admin（固定路径）；
3. blog（`/`、`/article/<slug>` 与评论写操作）；
4. seo（`/robots.txt`、`/sitemap.xml`，文章条目由这里注入）；
5. pages（`/theme` 与 `/<slug>` 兜底）——**最后**，否则兜底会遮蔽固定路径。
"""
from __future__ import annotations

import logging
import os
from functools import partial
from pathlib import Path

from . import db
from .core import assets
from .core.app import App
from .core.context import set_user_count_provider
from .core.session import load_user
from .modules.admin import routes as admin_routes
from .modules.auth import routes as auth_routes
from .modules.blog import logic as blog_logic
from .modules.blog import routes as blog_routes
from .modules.pages import logic as pages_logic
from .modules.pages import routes as pages_routes
from .modules.seo import routes as seo_routes
from .modules.system import routes as system_routes
from .modules.users import routes as users_routes

logger = logging.getLogger(__name__)

#: 启动钩子：数据库/模板就绪后按序执行（模块的内容缓存预热）。
STARTUP_HOOKS = (
    ("Articles loaded", blog_logic.load_articles),
    ("Custom pages loaded", pages_logic.load_pages),
)


def resolve_db_path() -> Path:
    """数据库文件路径（**装配层职责**）。

    优先级：`ELENVIND_DB` 环境变量 > `config.toml` 的 `database` > `<项目根>/sqlite.db`，
    相对路径一律相对项目根解析。

    这段逻辑以前在 `db` 层里，导致"持久化基座"反向依赖配置层；
    现在 db 只接受解析好的路径（`db.configure()`），配置语义留在装配层。
    """
    from .core.config import ROOT, config, resolve_path

    override = os.environ.get("ELENVIND_DB")
    if override:
        return Path(override)
    configured = config.get("database")
    if isinstance(configured, str) and configured.strip():
        return resolve_path(configured, ROOT / "sqlite.db")
    return ROOT / "sqlite.db"


def prepare_database() -> None:
    """数据库启动步骤：配置路径/窗口 → 建表与迁移 → 清理 → 记录库信息。

    由 `core.lifespan.startup(prepare_database=…)` 调用：core 只提供"何时执行"，
    "怎么初始化数据库"属于装配层 + db 层。
    """
    from .core.config import config

    warn_on_exposed_bind()
    db.configure(resolve_db_path())
    # 会话窗口实时从 config 取（db 不认识 config，只接受这个 callable）
    db.session.set_limits_provider(lambda: (
        config.get("session_absolute_days", db.DEFAULT_ABSOLUTE_DAYS),
        config.get("session_idle_days", db.DEFAULT_IDLE_DAYS),
    ))
    db.init_db()
    warn_about_missing_admin()

    removed_sessions = db.cleanup_expired_sessions()
    db.cleanup_old_login_attempts(days=30)
    db.cleanup_old_comment_attempts(days=7)
    db.cleanup_old_attempts(days=7)

    logger.info("Database: %s (schema v%s, journal_mode=%s, write lock %s)",
                db.DB_PATH, db.SCHEMA_VERSION, db.journal_mode(),
                db.lock_path_for(db.DB_PATH))
    logger.info("Expired sessions and rate-limit history cleaned "
                "(%s session(s) removed)", removed_sessions)


#: 回环地址：只有监听这些地址时，"应用端口不可从公网直连"才成立
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def warn_on_exposed_bind() -> None:
    """启动告警：**实际**监听非回环地址、同时信任转发头。

    这种组合下，任何能连到应用端口的人都**绕过**了反向代理：
    他不会是受信代理（因此骗不到 client IP / https 判定，这层是安全的），
    但 TLS、HSTS、反代侧的限速与访问控制全都被跳过。
    这是部署配置问题，不是代码缺陷 —— 所以只告警，不拒绝启动。

    host 取**实际 bind**（`run.py` 通过 `config.set_effective_bind()` 注入），
    只在直接 `gunicorn elenvind.wsgi:application` 时才回落配置值。早期版本只读
    `[server].host`：`--bind 0.0.0.0` 覆盖配置时告警会**静默消失**（假阴性），
    反过来 `--bind 127.0.0.1` 时又会误报（假阳性）。
    """
    from .core.config import config, effective_bind_host

    server = config.get("server") or {}
    host = effective_bind_host() or str(server.get("host", "127.0.0.1") or "").strip()
    trusted = server.get("trusted_proxies") or []
    if host in _LOOPBACK_HOSTS or not trusted:
        return
    logger.warning(
        "Listening on %s while trusting forwarded headers from %s: clients that can "
        "reach this port bypass the reverse proxy (TLS/HSTS/edge limits). Bind to "
        "127.0.0.1 and let the proxy forward, or firewall the port.",
        host, trusted)


def warn_about_missing_admin() -> None:
    """启动告警：`admin_user_id` 指向不存在/已注销的用户。

    管理员身份的唯一来源是配置里的那个 id；配置写错（或该账号后来注销）时结果
    不是报错，而是**静默地没有管理员** —— 评论区被刷屏时没人能涂黑。这里在启动
    日志里点名，避免"以为自己是管理员"（尤其新库还没注册时）。
    """
    from .core.security import admin_id

    owner = admin_id()
    if owner is None:
        logger.warning("No administrator configured (admin_user_id missing, null, "
                       "or invalid): nobody can moderate comments")
        return
    if db.get_user_by_id(owner) is None:
        logger.warning("admin_user_id=%s does not match an active user: nobody can "
                       "moderate comments. Register that account, or point "
                       "admin_user_id at an existing user id.", owner)


def create_app() -> App:
    """构造应用：把 Core App、错误页、会话加载与各模块装到一起。"""
    # Core 不认识 db：用户总数由这里注入一个数据源
    set_user_count_provider(db.get_user_number)

    app = App(
        not_found=system_routes.not_found,
        forbidden=system_routes.forbidden,
        server_error=system_routes.server_error,
        bad_request=system_routes.bad_request,
        # 会话 → 用户：Core 的 HTTP 层 + 注入的持久化 store
        user_loader=partial(load_user, store=db),
    )

    router = app.router
    assets.register(router)
    auth_routes.register(router)
    users_routes.register(router)
    admin_routes.register(router)
    blog_routes.register(router, render_not_found=system_routes.not_found)
    # 模块之间不互相 import：seo 需要的文章条目由这里显式注入
    seo_routes.register(router, articles=blog_logic.sitemap_articles)
    pages_routes.register(router, render_not_found=system_routes.not_found)
    return app


app = create_app()

__all__ = ["STARTUP_HOOKS", "app", "create_app", "prepare_database",
           "resolve_db_path", "warn_about_missing_admin", "warn_on_exposed_bind"]
