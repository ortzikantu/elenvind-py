"""开发/生产启动入口。

启动前先做一次与 lifespan 相同的配置加载与校验：配置不合法（site_url 非法、
端口越界等）时立刻以非零码退出并打印原因，而不是先起一个
"看起来在跑但每次请求都出错"的进程。

不需要任何环境变量：本应用没有签名密钥（会话是服务端随机 token）。
"""
import sys

import uvicorn

from elenvind.app import app
from elenvind.core.config import ConfigError, apply_runtime_config, config, load_config, validate_config
from elenvind.core.console import banner, error, info
from elenvind.core.version import get_version


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

    # 代理信任**只能有一个来源**：`[server].trusted_proxies`。
    #
    # 曾经这里硬编码 `forwarded_allow_ips="127.0.0.1"`，而应用自己按
    # `[server].trusted_proxies`（默认还多了 `::1`）判断是否采信转发头。
    # 两份列表不一致会导致两种真实故障：
    #   1. 反代不在回环（config 里明确让你改成代理真实地址）时，uvicorn 不重写
    #      scheme -> `request.is_secure()` 为假 -> **会话 Cookie 丢掉 Secure、
    #      HSTS 也不下发**；
    #   2. 反代走 `::1` 时应用信、uvicorn 不信 -> 上面那条 + 应用直接采信
    #      客户端可伪造的 `X-Forwarded-For` 首项 -> 登录/注册/评论的 IP 维度
    #      限流可被绕过。
    # 现在把同一份列表同时交给 uvicorn 与应用，两者不可能再漂移。
    trusted = server_config.get("trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(trusted, str):
        trusted = [item.strip() for item in trusted.split(",") if item.strip()]
    forwarded_allow_ips = ",".join(trusted) if trusted else ""

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
    info(f"Trusted proxies (single source): {forwarded_allow_ips or '(none)'}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=bool(forwarded_allow_ips),
        forwarded_allow_ips=forwarded_allow_ips,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
