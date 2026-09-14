import uvicorn
from elenvind.app import app
from elenvind.config import load_config, config
from elenvind.console import banner, info, warning
from elenvind.version import get_version

if __name__ == "__main__":
    try:
        load_config()
    except Exception as e:
        warning(f"Failed to load config: {e}, using defaults")
        server_config = {}
    else:
        server_config = config.get("server", {})

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
