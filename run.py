"""开发/生产启动入口（Gunicorn）。

    python run.py                    # 按 config.toml 的 [server] 启动 Gunicorn
    python run.py --reload           # 开发：代码改动自动重载 worker
    python run.py --workers 4        # 临时覆盖 worker 数
    python run.py --bind 0.0.0.0:8080
    python run.py --preload          # master 里导入应用（startup 只跑一次）

启动前先做一次与 `core.lifespan.startup()` 相同的配置加载与校验：配置不合法
（site_url 非法、端口越界等）时立刻以非零码退出并打印原因，而不是先起一个
"看起来在跑但每次请求都出错"的进程。

生产环境也可以绕过本脚本直接用 Gunicorn：

    gunicorn --workers 2 --bind 127.0.0.1:6789 elenvind.wsgi:application

代理信任**只有一个来源**：`[server].trusted_proxies`。本脚本把它作为
Gunicorn 的 `forwarded_allow_ips`，应用自己再用**同一份**列表判定客户端 IP
（X-Forwarded-For）与请求 scheme（X-Forwarded-Proto）—— 两层永远指向
同一个列表，不可能漂移。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from elenvind.core.config import (
    ROOT,
    ConfigError,
    apply_runtime_config,
    config,
    load_config,
    set_effective_bind,
    validate_config,
)
from elenvind.core.console import banner, error, info
from elenvind.core.version import get_version

USAGE = """\
Elenvind — Gunicorn 启动器

用法: python run.py [选项]

选项:
  --workers N         worker 进程数（默认 config.toml [server].workers，出厂值 2）
  --bind HOST:PORT    监听地址（默认 config.toml [server].host / port）
  --reload            代码改动自动重载（开发用；会显著变慢）
  --preload           在 master 进程导入应用（startup 只执行一次）
  --access-log        打开访问日志（默认关闭）
  --backup            在线备份数据库后退出（默认 backups/elenvind-<时间戳>.db）
  --backup-to PATH    与 --backup 同用：指定备份文件路径
  --help              显示本帮助

不用本脚本时：
  gunicorn --workers 2 --bind 127.0.0.1:6789 elenvind.wsgi:application
"""


def _parse_args(argv):
    """解析命令行参数；不认识/缺值的选项一律报错（不做静默忽略）。"""
    options = {"workers": None, "bind": None, "reload": False,
               "preload": False, "access_log": False, "help": False,
               "backup": False, "backup_to": None}
    index = 0
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg in ("-h", "--help"):
            options["help"] = True
        elif arg == "--reload":
            options["reload"] = True
        elif arg == "--preload":
            options["preload"] = True
        elif arg == "--access-log":
            options["access_log"] = True
        elif arg == "--backup":
            options["backup"] = True
        elif arg in ("--workers", "--bind", "--backup-to"):
            if index >= len(argv):
                raise ValueError(f"{arg} 需要一个取值")
            value = argv[index]
            index += 1
            if arg == "--workers":
                try:
                    options["workers"] = int(value)
                except ValueError:
                    raise ValueError(f"--workers 需要整数，收到 {value!r}") from None
                if options["workers"] < 1:
                    raise ValueError("--workers 必须 >= 1")
            elif arg == "--bind":
                if ":" not in value:
                    raise ValueError(f"--bind 需要 HOST:PORT 形式，收到 {value!r}")
                options["bind"] = value
            else:
                options["backup_to"] = value
                options["backup"] = True
        else:
            raise ValueError(f"未知选项: {arg}")
    return options


def _trusted_proxies(server_config):
    """`[server].trusted_proxies` -> (Gunicorn 用的逗号串, 展示用列表)。"""
    trusted = server_config.get("trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(trusted, str):
        trusted = [item.strip() for item in trusted.split(",") if item.strip()]
    if not isinstance(trusted, list):
        trusted = []
    entries = [str(item).strip() for item in trusted if str(item).strip()]
    return ",".join(entries), entries


def _run_backup(destination=None) -> int:
    """`--backup` / `--backup-to`：把数据库复制成一份一致副本后退出。

    备份能力在 `db.backup`（SQLite 的 backup API）：WAL 模式下裸拷贝主文件会
    丢掉还在 `-wal` 里的事务，而 `cp` 也不是原子快照。这里不启动 Gunicorn ——
    备份是运维动作，不该顺带起一个服务。
    """
    from elenvind import db  # 延迟导入：只有真的备份时才加载 db 层
    from elenvind.app import resolve_db_path

    source = resolve_db_path()
    if destination:
        target = Path(destination).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = ROOT / "backups" / f"elenvind-{stamp}.db"
    try:
        written = db.backup_to(target, db_path=source)
    except db.BackupError as problem:
        error(str(problem))
        return 1
    info(f"Backup written: {written}")
    info("Restore: stop the service, move the current sqlite.db away, then put the "
         "backup in its place (and delete any sqlite.db-wal / sqlite.db-shm).")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        options = _parse_args(argv)
    except ValueError as problem:
        error(str(problem))
        print(USAGE, file=sys.stderr)
        return 2

    if options["help"]:
        print(USAGE)
        return 0

    try:
        load_config()
        validate_config()
        apply_runtime_config()
    except ConfigError as e:
        error(f"Invalid configuration: {e}")
        return 2

    if options["backup"]:
        return _run_backup(options["backup_to"])

    server_config = config.get("server", {}) or {}
    host = server_config.get("host", "127.0.0.1")
    port = server_config.get("port", 6789)
    workers = options["workers"] or int(server_config.get("workers", 2) or 2)
    bind = options["bind"] or f"{host}:{port}"
    forwarded_allow_ips, trusted = _trusted_proxies(server_config)
    # 把**实际**监听地址交给应用层：暴露告警必须看真实 bind，而不是配置意图
    # （`--bind 0.0.0.0` 覆盖配置时，只读配置的告警会静默失效）。
    set_effective_bind(bind)

    banner("=================================")
    banner("")
    banner("  ┏┓┓      •   ┓")
    banner("  ┣ ┃┏┓┏┓┓┏┓┏┓┏┫")
    banner("  ┗┛┗┗ ┛┗┗┛┗┛┗┗┻")
    banner("  A Personal Website Server")
    banner(f"  {get_version()}")
    banner("  Elen síla lúmenn' omentielvo.")
    banner("")
    banner("=================================")
    info(f"Starting Gunicorn (WSGI) at http://{bind} with {workers} worker(s)")
    info(f"Trusted proxies (single source): {', '.join(trusted) or '(none)'}")

    from gunicorn.app.base import BaseApplication

    class ElenvindServer(BaseApplication):
        """把 config.toml 的 [server] 段翻译成 Gunicorn 配置。"""

        def __init__(self, settings):
            self.settings = settings
            super().__init__()

        def load_config(self):
            for key, value in self.settings.items():
                if value is None:
                    continue
                if key not in self.cfg.settings:
                    raise ValueError(f"unknown gunicorn setting: {key}")
                self.cfg.set(key, value)

        def load(self):
            # 导入即完成 startup()（配置/日志/i18n/模板/建库/缓存预热）；
            # 失败会在这里抛异常，Gunicorn 拒绝启动 —— 不会跑半初始化的站点。
            from elenvind.wsgi import application

            return application

    ElenvindServer({
        "bind": bind,
        "workers": workers,
        "worker_class": "sync",
        "preload_app": True if options["preload"] else None,
        "reload": True if options["reload"] else None,
        "accesslog": "-" if options["access_log"] else None,
        "forwarded_allow_ips": forwarded_allow_ips,
        # 与旧实现一致：日志级别 warning，访问日志默认关闭
        "loglevel": "warning",
        "proc_name": "elenvind",
    }).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
