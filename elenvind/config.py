"""配置加载与校验。

约定：
- 唯一配置源是项目根目录的 config.toml；只在启动（lifespan）阶段加载一次，
  运行期修改文件不会热生效——所有业务模块都直接 `from .config import config`
  读取同一份已校验过的字典。
- 路径类配置一律相对**项目根目录**解析（不是当前工作目录），
  避免 systemd / 手工启动时 cwd 不同导致读写到不同文件。
- 配置错误在启动时一次性暴露（validate_config 抛 ConfigError，服务拒绝启动），
  而不是运行到某个页面才 500。
"""
import tomllib
from pathlib import Path
from urllib.parse import urlparse

# 项目根目录：elenvind/config.py -> 上一级的上一级
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

config = {}


class ConfigError(ValueError):
    """配置缺失/非法：启动阶段直接失败，不做静默兜底。"""


def load_config():
    """读取并解析 config.toml，返回配置字典（同时更新模块级 config）。"""
    if not CONFIG_PATH.exists():
        raise ConfigError(f"Configuration file not found: {CONFIG_PATH}")
    try:
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {CONFIG_PATH}: {e}") from e
    config.clear()
    config.update(data)
    return config


def resolve_path(value, default: Path) -> Path:
    """把配置里的路径统一解析到项目根目录下；留空时用 default。"""
    if value is None or str(value).strip() == "":
        return default
    path = Path(str(value).strip())
    return path if path.is_absolute() else ROOT / path


# ======================= 校验工具 =======================

def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _check_int(value, name, low, high):
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{name} must be an integer, got {value!r}")
    _require(low <= value <= high, f"{name} must be between {low} and {high}, got {value}")


def _check_http_url(value, name, allow_empty=True):
    if value is None or str(value).strip() == "":
        _require(allow_empty, f"{name} must not be empty")
        return
    parsed = urlparse(str(value).strip())
    _require(parsed.scheme in ("http", "https") and bool(parsed.netloc),
             f"{name} must be an absolute http(s) URL, got {value!r}")


def _check_asset_url(value, name):
    """静态资源地址：允许空、站内绝对路径（/x）或 http(s) 绝对 URL。"""
    if value is None or str(value).strip() == "":
        return
    text = str(value).strip()
    if text.startswith("//"):
        raise ConfigError(f"{name} must not be a protocol-relative URL, got {value!r}")
    if text.startswith("/"):
        return
    _check_http_url(text, name, allow_empty=False)


def _check_comment_limits(raw, name):
    _require(isinstance(raw, dict), f"[{name}] must be a table")
    for key in ("max_per_user", "max_per_ip", "window_seconds"):
        _check_int(raw.get(key, 0), f"[{name}].{key}", 1, 100000)


def _check_rate(raw, name, keys):
    _require(isinstance(raw, dict), f"[{name}] must be a table")
    for key in keys:
        _check_int(raw.get(key, 0), f"[{name}].{key}", 1, 1000000)


# ======================= 启动校验 =======================

def validate_config(cfg=None):
    """一次性校验全部配置项；返回 None，非法配置抛 ConfigError。

    覆盖：监听地址/端口、站点 URL、管理员、数据库与内容目录、请求体上限、
    评论限制、各维度限流、日志、Cookie 前缀、代理白名单。
    """
    cfg = config if cfg is None else cfg

    # ----- 站点与语言 -----
    locale = str(cfg.get("locale", "en") or "").lower()
    base_locale = locale.split("-")[0].split("_")[0]
    _require(base_locale in ("en", "zh", "ja"),
             f"locale must be one of en/zh/ja (got {cfg.get('locale')!r})")
    _require(str(cfg.get("title", "") or "").strip() != "", "title must not be empty")
    _check_int(cfg.get("max_length", 1000), "max_length", 1, 100000)
    _check_int(cfg.get("max_comment_depth", 32), "max_comment_depth", 1, 10000)
    _check_int(cfg.get("max_comments_per_article", 1000), "max_comments_per_article", 1, 1000000)

    # 管理员：必须是正整数；缺失即"无管理员"（不静默回退到 id=1）
    admin_user_id = cfg.get("admin_user_id", 1)
    if admin_user_id is not None:
        _check_int(admin_user_id, "admin_user_id", 1, 2 ** 31 - 1)

    # 注册开关
    _require(isinstance(cfg.get("registration_enabled", True), bool),
             "registration_enabled must be a boolean")

    # ----- 站点对外地址（canonical / robots / sitemap 唯一来源） -----
    site_url = str(cfg.get("site_url", "") or "").strip()
    if site_url:
        _check_http_url(site_url, "site_url", allow_empty=False)
        _require(not site_url.endswith("/"), "site_url must not end with '/'")

    # ----- 服务监听 -----
    server = cfg.get("server", {})
    _require(isinstance(server, dict), "[server] must be a table")
    host = server.get("host", "127.0.0.1")
    _require(isinstance(host, str) and host.strip() != "", "server.host must be a non-empty string")
    _check_int(server.get("port", 6789), "server.port", 1, 65535)
    trusted = server.get("trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(trusted, str):
        trusted = [item.strip() for item in trusted.split(",") if item.strip()]
    _require(isinstance(trusted, list) and all(isinstance(item, str) for item in trusted),
             "server.trusted_proxies must be a list of strings")
    _require(isinstance(server.get("cookie_prefix", False), bool),
             "server.cookie_prefix must be a boolean")

    # ----- 数据库与请求体 -----
    _require(isinstance(cfg.get("database", "sqlite.db"), str), "database must be a string path")
    _check_int(cfg.get("max_body_size", 1024 * 1024), "max_body_size", 1024, 64 * 1024 * 1024)

    # ----- 内容目录 -----
    for key, default in (("articles_dir", "articles"), ("usrpagess_dir", "usrpages")):
        raw = cfg.get(key, default)
        _require(isinstance(raw, str) and raw.strip() != "", f"{key} must be a non-empty path")
        path = resolve_path(raw, ROOT / default)
        _require(not path.exists() or path.is_dir(), f"{key} is not a directory: {path}")

    # ----- 静态资源（会进入 HTML 属性） -----
    static_cfg = cfg.get("static", {})
    _require(isinstance(static_cfg, dict), "[static] must be a table")
    for key in ("css", "favicon", "logo", "hero"):
        _check_asset_url(static_cfg.get(key), f"static.{key}")

    # ----- 分页 -----
    pagination = cfg.get("pagination", {})
    _require(isinstance(pagination, dict), "[pagination] must be a table")
    _check_int(pagination.get("per_page", 10), "pagination.per_page", 1, 1000)

    # ----- 评论限制与限流 -----
    _check_comment_limits(cfg.get("comment_limits", {
        "max_per_user": 5, "max_per_ip": 10, "window_seconds": 60}), "comment_limits")
    _check_rate(cfg.get("login_limits", {
        "max_email_failures": 5, "email_window_seconds": 86400,
        "max_ip_failures": 20, "ip_window_seconds": 900,
        "max_global_failures": 200, "global_window_seconds": 900}), "login_limits",
        ("max_email_failures", "email_window_seconds", "max_ip_failures",
         "ip_window_seconds", "max_global_failures", "global_window_seconds"))
    _check_rate(cfg.get("register_limits", {
        "max_per_ip": 5, "window_seconds": 3600}), "register_limits",
        ("max_per_ip", "window_seconds"))

    # ----- 日志 -----
    logging_cfg = cfg.get("logging", {})
    _require(isinstance(logging_cfg, dict), "[logging] must be a table")
    _require(str(logging_cfg.get("level", "info")).lower() in
             ("debug", "info", "warning", "error", "critical"),
             f"logging.level must be debug/info/warning/error/critical, got {logging_cfg.get('level')!r}")
    _require(isinstance(logging_cfg.get("file", "logs/app.log"), str),
             "logging.file must be a string path")
    _check_int(logging_cfg.get("max_bytes", 10 * 1024 * 1024), "logging.max_bytes", 4096, 2 ** 40)
    _check_int(logging_cfg.get("backup_count", 5), "logging.backup_count", 0, 1000)

    # ----- 部署前置条件（历史约定，保留为显式启动门禁） -----
    import os
    _require(bool(os.environ.get("SECRET_KEY")),
             "SECRET_KEY environment variable is not set")

    return None


def apply_runtime_config(cfg=None):
    """把需要影响其它模块的配置项下发到对应模块（Cookie 前缀等）。"""
    from .security import configure_cookie_prefix
    cfg = config if cfg is None else cfg
    cookie_prefix = bool(cfg.get("server", {}).get("cookie_prefix", False))
    configure_cookie_prefix(cookie_prefix)


# ======================= 常用取值 helper =======================

def get(path_keys, default=None):
    """按键路径读取配置：get(("server", "port"), 6789)。"""
    node = config
    for key in path_keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
