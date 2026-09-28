"""开发/生产启动入口。

启动前先做一次与 lifespan 相同的配置加载与校验：配置不合法（缺 SECRET_KEY、
site_url 非法、端口越界等）时立刻以非零码退出并打印原因，而不是先起一个
"看起来在跑但每次请求都出错"的进程。
"""
import sys

import uvicorn

from elenvind.app import app
from elenvind.config import ConfigError, apply_runtime_config, config, load_config, validate_config
from elenvind.console import banner, error, info
from elenvind.version import get_version


def main() -> int:
    try:
        load_config()
        validate_config()
        apply_runtime_config()
    except ConfigError as e:
        error(f"Invalid configuration: {e}")
        return 2

    server_config = config.get("server", {}) or {}
    host = server_config.get("host", "127.0.0.1")
    port = server_config.get("port", 6789)

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
    info(f"Starting server at http://{host}:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
