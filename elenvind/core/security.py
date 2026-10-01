"""安全原语：密码哈希、CSRF 令牌与 Cookie 构造。

职责说明：
- 本模块只做“密码学原语 + Cookie 构造”，不做数据库操作；
  会话 token 的持久化见 db_session.py。
- 本模块导入时不读写文件、不读取环境变量（便于测试与审计）。
- **不需要任何签名密钥**：会话是服务端随机 token，CSRF 是双提交 Cookie，
  密码是自描述哈希。没有任何东西需要"用一个密钥去签"。配置校验见
  config.validate_config()。

密码哈希格式（自描述，支持长期升级迁移）：

    scrypt$ln=15,r=8,p=1$<salt hex>$<digest hex>          <- 当前写入格式
    pbkdf2_sha256$600000$<salt hex>$<digest hex>          <- 可写入/可校验
    <salt hex>$<digest hex>                               <- 历史格式（仅校验，等价 PBKDF2-SHA256 10 万次）

旧格式使用者在下次登录成功后会被透明地重新哈希为新格式（渐进式 rehash，
见 password_needs_rehash），无需强制全员改密。
"""
import hashlib
import hmac
import os
import re
import secrets
from http.cookies import SimpleCookie

# ----- Cookie 名称与生命周期 -----
# 名称可在启动时切换为 __Host- 前缀（config.toml [server].cookie_prefix = true）。
# __Host- 前缀由浏览器强制要求 Secure + Path=/ + 无 Domain，能挡子域名写 Cookie 的攻击；
# 但它依赖全程 HTTPS，因此默认关闭，仅 HTTPS 部署才应开启。
# 读取时始终同时接受带前缀与不带前缀两种名字，切换配置不会把所有人踢下线。
SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
THEME_COOKIE = "theme"              # 手动白/夜主题偏好（light/dark），无该 Cookie 时跟随系统
SESSION_MAX_AGE = 7 * 86400      # 会话 Cookie 7 天，与 db_session.SESSION_DAYS 保持同一数值
CSRF_MAX_AGE = 30 * 86400        # CSRF Cookie 30 天（仅做同源比对，无状态）
THEME_MAX_AGE = 365 * 86400      # 主题 Cookie 1 年

_COOKIE_PREFIX = ""


def configure_cookie_prefix(enabled: bool) -> None:
    """启用/停用 __Host- 前缀（由 config 校验阶段调用一次）。"""
    global _COOKIE_PREFIX
    _COOKIE_PREFIX = "__Host-" if enabled else ""


def cookie_name(base: str) -> str:
    """返回带当前前缀的 Cookie 名（写入时使用）。"""
    return _COOKIE_PREFIX + base


def cookie_names(base: str) -> tuple:
    """返回该 Cookie 的全部可接受名称（写入名在前，兼容名在后）。"""
    canonical = _COOKIE_PREFIX + base
    if _COOKIE_PREFIX and canonical != base:
        return (canonical, base)
    return (canonical,)


def pick_cookie(cookies: dict, base: str):
    """从请求 Cookie 中取出该逻辑 Cookie 的值，不存在返回 None。"""
    for name in cookie_names(base):
        if name in cookies:
            return cookies[name]
    return None


# ----- 输入长度策略（注册 / 登录 / 改密 / 资料修改统一引用） -----
PASSWORD_MIN = 8
PASSWORD_MAX = 128
NICKNAME_MAX = 50
EMAIL_MAX = 254

# ----- 密码哈希参数（本机 benchmark 结果：scrypt N=2^15, r=8, p=1 ≈ 240 ms/次） -----
SCRYPT_N = 2 ** 15           # CPU/内存代价主参数
SCRYPT_R = 8                 # 块大小
SCRYPT_P = 1                 # 并行度
SCRYPT_DKLEN = 32            # 派生密钥长度
SCRYPT_MAXMEM = 64 * 1024 * 1024   # 允许的最大内存（N=2^15,r=8 实际约 33 MB）
PBKDF2_ITERATIONS = 600_000  # 本机 ≈ 134 ms/次（历史格式为 10 万次）
SALT_BYTES = 16

_SCRYPT_ALGO = "scrypt"
_PBKDF2_ALGO = "pbkdf2_sha256"
_LEGACY_PBKDF2_ITERATIONS = 100_000   # 历史格式固定参数，仅用于校验旧哈希
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _scrypt(password: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    """scrypt 派生；maxmem 随参数放大，避免 OpenSSL 默认 32 MB 上限误伤。"""
    maxmem = 132 * n * r + 1024 * 1024
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p,
                          maxmem=maxmem, dklen=SCRYPT_DKLEN)


def _encode_scrypt_params(n: int, r: int, p: int) -> str:
    return f"ln={n.bit_length() - 1},r={r},p={p}"


def _parse_scrypt_params(raw: str):
    """解析 "ln=15,r=8,p=1"；非法参数返回 None（视为无法识别的哈希）。"""
    values = {}
    for part in raw.split(","):
        key, _, value = part.partition("=")
        if not value or not value.isdigit():
            return None
        values[key.strip()] = int(value)
    if set(values) != {"ln", "r", "p"}:
        return None
    try:
        n = 1 << values["ln"]
    except (ValueError, OverflowError):
        return None
    r, p = values["r"], values["p"]
    # 参数边界：防止恶意/损坏的数据库值触发天量内存或极长计算
    if not (1 <= values["ln"] <= 20) or not (1 <= r <= 32) or not (1 <= p <= 16):
        return None
    return n, r, p


def _parse_hash(stored_hash):
    """把存储的哈希串解析为 (算法, 参数, 盐, 摘要)。

    无法识别的格式返回 None；调用方据此判定"校验失败"，而不是抛异常。
    - 4 段：algorithm$params$salt$digest
    - 2 段：历史格式 salt$digest（等价 pbkdf2_sha256 + 10 万次）
    """
    if not isinstance(stored_hash, str) or not stored_hash:
        return None
    parts = stored_hash.split("$")
    try:
        if len(parts) == 4:
            algo, params, salt_hex, digest_hex = parts
        elif len(parts) == 2:
            algo, params = _PBKDF2_ALGO, str(_LEGACY_PBKDF2_ITERATIONS)
            salt_hex, digest_hex = parts
        else:
            return None
        if not _HEX_RE.match(salt_hex) or not _HEX_RE.match(digest_hex):
            return None
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return None
    if not salt or not digest:
        return None
    return algo, params, salt, digest


def hash_password(password: str) -> str:
    """生成自描述密码哈希串：scrypt$ln=..,r=..,p=..$盐$摘要。"""
    salt = os.urandom(SALT_BYTES)
    digest = _scrypt(password.encode("utf-8"), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"{_SCRYPT_ALGO}${_encode_scrypt_params(SCRYPT_N, SCRYPT_R, SCRYPT_P)}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash) -> bool:
    """校验密码。只吞掉"格式/参数不可识别"这类预期错误，程序缺陷照常抛出。"""
    parsed = _parse_hash(stored_hash)
    if parsed is None:
        return False
    algo, params, salt, expected = parsed
    try:
        raw = password.encode("utf-8")
        if algo == _SCRYPT_ALGO:
            scrypt_params = _parse_scrypt_params(params)
            if scrypt_params is None:
                return False
            candidate = _scrypt(raw, salt, *scrypt_params)
        elif algo == _PBKDF2_ALGO:
            if not params.isdigit():
                return False
            candidate = hashlib.pbkdf2_hmac("sha256", raw, salt, int(params))
        else:
            return False   # 未知算法（未来版本写入的哈希）：不通过，交由升级路径处理
    except (ValueError, TypeError):
        # 参数非法（如 ln 超界导致 scrypt 抛 ValueError）；不是程序缺陷
        return False
    return hmac.compare_digest(candidate, expected)


def password_needs_rehash(stored_hash) -> bool:
    """判断存储的哈希是否应升级为新格式（未知格式/旧参数一律视为需要）。"""
    parsed = _parse_hash(stored_hash)
    if parsed is None:
        return True
    algo, params, _salt, _digest = parsed
    if algo != _SCRYPT_ALGO:
        return True
    return _parse_scrypt_params(params) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


#: 哑哈希：账号不存在时用来"陪跑"一次等量校验，弱化计时侧信道。
#: 对应一个不会被使用的随机口令，仅用于消耗等量 CPU。
_DUMMY_HASH = None


def dummy_verify(password: str) -> bool:
    """对哑哈希执行一次真实校验（结果恒为 False，仅用于等量计时）。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("elenvind-dummy-password-for-timing")
    return verify_password(password, _DUMMY_HASH)


# ----- CSRF 保护（Double-Submit Cookie，无服务端状态） -----
# 随机令牌同时写入 Cookie 与表单隐藏域，校验时只做两者比对。
# 攻击者无法在跨站请求中同时伪造两个值：Cookie 为 HttpOnly 且 SameSite=Lax，
# 跨站 POST 请求根本不会携带该 Cookie。
_CSRF_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def generate_csrf_token() -> str:
    """生成 32 字节 URL-safe 随机令牌（固定 43 字符，便于格式校验）。"""
    return secrets.token_urlsafe(32)


def is_valid_csrf_token(token) -> bool:
    """校验令牌格式，防止把任意超长/异常字符串带入比较逻辑。

    用 fullmatch 而非 match：`$` 在 match 语义下允许结尾多一个换行，
    会让 "43 个合法字符 + \\n" 通过校验（表单值里带换行并非不可能）。
    """
    return isinstance(token, str) and _CSRF_PATTERN.fullmatch(token) is not None


def verify_csrf_token(submitted, expected) -> bool:
    """常量时间比对表单令牌与 Cookie 令牌。"""
    if not is_valid_csrf_token(submitted) or not is_valid_csrf_token(expected):
        return False
    return hmac.compare_digest(submitted.encode("ascii"), expected.encode("ascii"))


# ----- 管理员判定 -----
# 管理员身份只有一个来源：config.toml 顶层 admin_user_id（默认 1，即最早注册的账号）。
# 业务代码一律调用 is_admin()，不得再散落 "id == 1" 这类魔法数字。
# admin_user_id 缺失或非法时视为"无管理员"，而不是静默回退到 id=1——
# 静默回退会把权限悄悄授予一个意料之外的账号。
def admin_id():
    """返回当前配置的管理员用户 id；未配置/非法时返回 None。"""
    from .config import config   # 延迟导入：本模块导入期不依赖 config 加载状态
    value = config.get("admin_user_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def is_admin(user) -> bool:
    """判断用户是否为管理员（user 可为 sqlite3.Row、dict 或 None）。"""
    if not user:
        return False
    owner = admin_id()
    if owner is None:
        return False
    try:
        return int(user["id"]) == owner
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def is_admin_id(user_id) -> bool:
    """按 user_id 判断管理员身份（评论行只带 user_id 时使用）。"""
    owner = admin_id()
    if owner is None:
        return False
    try:
        return int(user_id) == owner
    except (TypeError, ValueError):
        return False


# ----- Cookie 构造 -----
def _make_cookie(name: str, value: str, secure: bool, max_age: int, http_only: bool = True) -> tuple:
    """构造 Set-Cookie 响应头（bytes 键值对，供 ASGI headers 使用）。"""
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    if http_only:
        cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Lax"
    cookie[name]["max-age"] = max_age
    if secure:
        cookie[name]["secure"] = True
    return (b"set-cookie", cookie[name].OutputString().encode("utf-8"))


def set_cookie_header(token: str, secure: bool = False, max_age: int = SESSION_MAX_AGE,
                      base: str = SESSION_COOKIE) -> tuple:
    """下发会话 Cookie（登录成功时使用）。"""
    return _make_cookie(cookie_name(base), token, secure, max_age)


def clear_cookie_headers(secure: bool = False, base: str = SESSION_COOKIE,
                         http_only: bool = True) -> list:
    """返回让该 Cookie 立即过期的全部 Set-Cookie 头。

    启用 __Host- 前缀后会同时清理旧的无前缀 Cookie，避免切换配置时残留旧值。
    """
    return [_make_cookie(name, "", secure, 0, http_only=http_only) for name in cookie_names(base)]



def clear_session_cookie(secure: bool = False, base: str = SESSION_COOKIE) -> tuple:
    """让客户端会话 Cookie 立即过期（登出/删号/改密后使用，单个头）。"""
    return _make_cookie(cookie_name(base), "", secure, 0)


# ----- 安全响应头（全项目唯一定义处） -----
# 站点零 JS：script-src 'none' 直接掐断 XSS 执行链。
# style-src 的 'unsafe-inline' 是历史遗留（内联样式）；新模板已全部类化，
# 后续可收紧——但收紧前必须先确认没有任何视图再依赖内联样式。
CSP = (
    "default-src 'none'; script-src 'none'; "
    "style-src 'self' 'unsafe-inline' http: https:; "
    "img-src 'self' data: http: https:; media-src 'self' http: https:; "
    "font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; manifest-src 'none'"
)

#: 每个响应都会带上的安全头
BASE_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"content-security-policy", CSP.encode("utf-8")),
    (b"permissions-policy",
     b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

#: 仅 HTTPS 请求追加
HSTS_HEADER = (b"strict-transport-security", b"max-age=31536000")

def csrf_cookie_header(token: str, secure: bool = False, max_age: int = CSRF_MAX_AGE,
                       base: str = CSRF_COOKIE) -> tuple:
    """下发或续期 CSRF Cookie。"""
    return _make_cookie(cookie_name(base), token, secure, max_age)


def theme_cookie_header(value: str, secure: bool = False, max_age: int = THEME_MAX_AGE) -> tuple:
    """下发主题偏好 Cookie（仅接受 light/dark，调用方需先校验）。"""
    return _make_cookie(THEME_COOKIE, value, secure, max_age)


def parse_cookies(scope) -> dict:
    """从 ASGI scope 的 Cookie 请求头解析为 {name: value}（同名取最后一个）。

    自己按 RFC 6265 的语法切分而不是交给 http.cookies.SimpleCookie：
    后者遇到一个畸形片段（如 "=broken"）会把**整个头部**的 Cookie 全部丢弃，
    导致一个损坏的无关 Cookie 让所有人掉线。这里只跳过畸形的那个片段。
    """
    cookies = {}
    for header in scope.get("headers", []):
        if header[0] != b"cookie":
            continue
        raw = header[1].decode("latin-1")
        for part in raw.split(";"):
            name, sep, value = part.partition("=")
            if not sep:
                continue
            name = name.strip()
            if not name or value.strip().startswith('"') or "=" in name:
                # 空名 / 带引号的值（浏览器不会这样发）/ 名字里再出现 "=" 都跳过
                continue
            cookies[name] = value.strip()
    return cookies
