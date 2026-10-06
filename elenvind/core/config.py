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
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

#: 控制字符（含 CR/LF）：任何进入响应头的配置值都不允许包含，
#: 否则可以伪造出额外的响应头。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: CSP 指令名必须是合法 token（RFC 7230）：小写字母、数字与连字符。
_CSP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# 项目根目录：elenvind/core/config.py -> 上溯三级（core -> elenvind -> 项目根）
ROOT = Path(__file__).resolve().parent.parent.parent
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


#: 静态资源 URL 里一律禁止的字符。
#: 理由：`static.hero` 会被写进内联样式 `style="background-image:url('…')"`，
#: 而浏览器解析 style 属性时**会先做 HTML 实体解码**，所以 `&#39;` 会变成真正的
#: `'` 从而闭合 `url('…')` 并注入任意 CSS（`background:url(//evil/?leak)`）。
#: 只靠 HTML 转义挡不住这种注入，必须在配置层把字符集收窄。
#: 这里排除了引号、圆括号、尖括号、反斜杠、空白与控制字符。
_FORBIDDEN_URL_CHARS = set("'\"()<>\\ \t")
#: 这些字符即便百分号编码也一律拒绝（双重编码绕过没有意义，只会是拼错）
_FORBIDDEN_AUTHORITY_CHARS = set("'\"()<>\\ \t@")


def _check_asset_url(value, name):
    """静态资源地址：允许空、站内绝对路径（/x）或 http(s) 绝对 URL。

    额外约束字符集（见 `_FORBIDDEN_URL_CHARS`）：这些值会进入 HTML 属性
    甚至内联 CSS 的 `url()`，必须保证无法闭合外层语法。
    """
    if value is None or str(value).strip() == "":
        return
    text = str(value).strip()
    for label, candidate in (("URL", text),
                             ("path", urlparse(text).path),
                             ("query", urlparse(text).query),
                             ("fragment", urlparse(text).fragment)):
        bad = sorted({ch for ch in candidate if ch in _FORBIDDEN_URL_CHARS})
        _require(not bad, f"{name} {label} contains forbidden characters {bad}: {value!r}")
        _require(not any(ord(ch) < 0x20 for ch in candidate),
                 f"{name} {label} contains control characters: {value!r}")
    if text.startswith("//"):
        raise ConfigError(f"{name} must not be a protocol-relative URL, got {value!r}")
    if text.startswith("/"):
        _require(urlparse(text).scheme == "",
                 f"{name} must not carry a scheme: {value!r}")
        return
    parsed = urlparse(text)
    _require(parsed.scheme in ("http", "https") and bool(parsed.netloc),
             f"{name} must be a site-relative path or an absolute http(s) URL, "
             f"got {value!r}")
    bad = sorted({ch for ch in parsed.netloc if ch in _FORBIDDEN_AUTHORITY_CHARS})
    _require(not bad, f"{name} host contains forbidden characters {bad}: {value!r}")


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

    # 管理员：必须是正整数。键缺失时默认 1（最早的账号）；显式 null 表示
    # "没有管理员"；其它非法值一律拒绝启动，避免"以为关掉了管理入口、
    # 其实配置没生效"这类静默失败。
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
    # Gunicorn worker 进程数。默认 2：多 worker 是设计的一部分
    # （Core 的 write_tx() 用 flock + BEGIN IMMEDIATE 串行化跨进程写），
    # 上限 64 只是防手滑写个天文数字把机器打满。
    _check_int(server.get("workers", 2), "server.workers", 1, 64)
    trusted = server.get("trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(trusted, str):
        trusted = [item.strip() for item in trusted.split(",") if item.strip()]
    _require(isinstance(trusted, list) and all(isinstance(item, str) for item in trusted),
             "server.trusted_proxies must be a list of strings")
    _require(isinstance(server.get("cookie_prefix", False), bool),
             "server.cookie_prefix must be a boolean")

    # ----- 安全响应头（Core 统一注入，见 core/security.build_security_headers） -----
    security = cfg.get("security", {}) or {}
    _require(isinstance(security, dict), "security must be a table")
    for key in ("csp_enabled", "hsts_enabled", "hsts_include_subdomains"):
        value = security.get(key)
        _require(value is None or isinstance(value, bool),
                 f"security.{key} must be a boolean")
    hsts_max_age = security.get("hsts_max_age", 31536000)
    _require(isinstance(hsts_max_age, int) and not isinstance(hsts_max_age, bool),
             "security.hsts_max_age must be an integer (seconds)")
    _require(0 <= hsts_max_age <= 63072000,
             "security.hsts_max_age must be between 0 and 63072000 (2 years)")

    policy = security.get("permissions_policy")
    _require(policy is None or isinstance(policy, str),
             "security.permissions_policy must be a string")
    _require(not isinstance(policy, str) or _CONTROL_RE.search(policy) is None,
             "security.permissions_policy must not contain control characters")

    csp = security.get("csp", {}) or {}
    _require(isinstance(csp, dict), "security.csp must be a table of directives")
    for name, value in csp.items():
        _require(isinstance(name, str) and name.strip() != "",
                 "security.csp directive names must be non-empty strings")
        # 指令名必须是合法 token：否则可能拼出畸形头（甚至注入换行）
        _require(_CSP_NAME_RE.match(name) is not None,
                 f"security.csp.{name} is not a valid directive name")
        if value is None or value is False:
            continue                        # 显式关闭该指令
        if isinstance(value, str):
            # 字符串形式按空白切分：既接受 "a b" 也接受单个值
            _require(_CONTROL_RE.search(value) is None,
                     f"security.csp.{name} must not contain control characters")
            continue
        _require(isinstance(value, list)
                 and all(isinstance(item, str) for item in value),
                 f"security.csp.{name} must be a list of strings or a string")
        for item in value:
            _require(_CONTROL_RE.search(item) is None,
                     f"security.csp.{name} values must not contain control characters")

    # ----- 数据库与请求体 -----
    _require(isinstance(cfg.get("database", "sqlite.db"), str), "database must be a string path")
    _check_int(cfg.get("max_body_size", 1024 * 1024), "max_body_size", 1024, 64 * 1024 * 1024)

    # ----- 会话过期（天数；0 = 该维度不过期） -----
    # 绝对过期是 token 泄露后的风险窗口硬上限，所以上限给到 10 年也基本等价于"不过期"；
    # 真正的"不过期"请显式写 0，这样意图清楚（并会在文档里看到风险提示）。
    _check_int(cfg.get("session_absolute_days", 30), "session_absolute_days", 0, 3650)
    _check_int(cfg.get("session_idle_days", 15), "session_idle_days", 0, 3650)

    # ----- 内容目录与模板目录 -----
    # 全部走同一套校验：非空路径 + 存在时必须是目录。
    # 注意 `templates_dir` 允许指向包内默认目录（elenvind/templates）。
    from .templating import DEFAULT_TEMPLATES_DIR
    for key, default in (("articles_dir", ROOT / "articles"),
                         ("custom_pages_dir", ROOT / "custom_pages"),
                         ("templates_dir", DEFAULT_TEMPLATES_DIR)):
        raw = cfg.get(key)
        if raw is None:
            path = default
        else:
            _require(isinstance(raw, str) and raw.strip() != "",
                     f"{key} must be a non-empty path")
            path = resolve_path(raw, default)
        _require(not path.exists() or path.is_dir(), f"{key} is not a directory: {path}")

    # ----- 静态资源（会进入 HTML 属性） -----
    static_cfg = cfg.get("static", {})
    _require(isinstance(static_cfg, dict), "[static] must be a table")
    for key in ("css", "favicon", "logo", "hero"):
        _check_asset_url(static_cfg.get(key), f"static.{key}")

    # ----- 内置样式表回落开关 -----
    # true（默认）：[static].css 为空时使用应用内置样式表；
    # false：为空就不输出 <link>（给"我有自己的样式方案"留出口）。
    _require(isinstance(cfg.get("use_builtin_css", True), bool),
             "use_builtin_css must be a boolean")

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

    # 说明：本应用**不需要任何签名密钥**。
    # 会话是服务端随机 token（存在 SQLite 里，客户端只拿到不可猜测的值），
    # CSRF 是双提交 Cookie（令牌本身就是随机值），密码是 scrypt 自描述哈希。
    # 因此这里没有 SECRET_KEY 之类的环境变量门禁 —— 那是纯仪式。
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
