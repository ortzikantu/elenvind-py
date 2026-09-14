"""安全原语：密码哈希、CSRF 令牌与 Cookie 构造。

职责说明：
- 本模块只做“密码学原语 + Cookie 构造”，不做数据库操作；
  会话 token 的持久化见 db_session.py。
- 密码：PBKDF2-HMAC-SHA256 + 每用户 16 字节随机盐，防彩虹表；校验用
  compare_digest 常量时间比较，防计时侧信道。
- CSRF：无状态 Double-Submit Cookie（随机令牌同时写入 Cookie 与表单，只比对两者），
  不依赖服务端会话状态，因此登出/换账号/改密清会话都不会使令牌失效。
- Cookie 均带 HttpOnly + SameSite=Lax；请求为 HTTPS（可信代理标记 scheme）时自动加 Secure。
"""
import hashlib
import hmac
import os
import re
import secrets
from http.cookies import SimpleCookie

# 历史约定：早期版本用 SECRET_KEY 做会话 token 签名；现架构改为服务端随机会话
# （见 db_session.py），SECRET_KEY 已无密码学用途，仅保留为“启动环境一致性门禁”：
# 统一要求部署方显式注入环境变量，避免隐式默认值带来的配置漂移。
if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY environment variable is not set")

# ----- Cookie 名称与生命周期 -----
SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
THEME_COOKIE = "theme"              # 手动白/夜主题偏好（light/dark），无该 Cookie 时跟随系统
SESSION_MAX_AGE = 7 * 86400      # 会话 Cookie 7 天，与 db_session.SESSION_DAYS 保持同一数值
CSRF_MAX_AGE = 30 * 86400        # CSRF Cookie 30 天（仅做同源比对，无状态）
THEME_MAX_AGE = 365 * 86400      # 主题 Cookie 1 年

# ----- 输入长度策略（注册 / 登录 / 改密 / 资料修改统一引用） -----
PASSWORD_MIN = 8
PASSWORD_MAX = 128
NICKNAME_MAX = 50
EMAIL_MAX = 254

# ----- 密码哈希 -----
def hash_password(password: str, salt: bytes = None) -> str:
    """PBKDF2 哈希，输出格式：<salt hex>$<digest hex>"""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码；任何格式异常都返回 False 而不是抛异常（避免信息泄露）。"""
    try:
        salt_hex, hash_hex = stored_hash.split('$')
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False

# ----- CSRF 保护（Double-Submit Cookie，无服务端状态） -----
# 随机令牌同时写入 Cookie 与表单隐藏域，校验时只做两者比对。
# 攻击者无法在跨站请求中同时伪造两个值：Cookie 为 HttpOnly 且 SameSite=Lax，
# 跨站 POST 请求根本不会携带该 Cookie。
_CSRF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def generate_csrf_token() -> str:
    """生成 32 字节 URL-safe 随机令牌（固定 43 字符，便于格式校验）。"""
    return secrets.token_urlsafe(32)


def is_valid_csrf_token(token) -> bool:
    """校验令牌格式，防止把任意超长/异常字符串带入比较逻辑。"""
    return isinstance(token, str) and bool(_CSRF_PATTERN.match(token))


def verify_csrf_token(submitted, expected) -> bool:
    """常量时间比对表单令牌与 Cookie 令牌。"""
    if not is_valid_csrf_token(submitted) or not is_valid_csrf_token(expected):
        return False
    return hmac.compare_digest(submitted.encode("ascii"), expected.encode("ascii"))

# ----- Cookie 构造 -----
def _make_cookie(name: str, value: str, secure: bool, max_age: int) -> tuple:
    """构造 Set-Cookie 响应头（bytes 键值对，供 ASGI headers 使用）。"""
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Lax"
    cookie[name]["max-age"] = max_age
    if secure:
        cookie[name]["secure"] = True
    return (b"set-cookie", cookie[name].OutputString().encode("utf-8"))


def set_cookie_header(token: str, secure: bool = False, max_age: int = SESSION_MAX_AGE) -> tuple:
    """下发会话 Cookie（登录成功时使用）。"""
    return _make_cookie(SESSION_COOKIE, token, secure, max_age)


def clear_session_cookie(secure: bool = False) -> tuple:
    """让客户端会话 Cookie 立即过期（登出/删号/改密后使用）。"""
    return _make_cookie(SESSION_COOKIE, "", secure, 0)


def csrf_cookie_header(token: str, secure: bool = False, max_age: int = CSRF_MAX_AGE) -> tuple:
    """下发或续期 CSRF Cookie。"""
    return _make_cookie(CSRF_COOKIE, token, secure, max_age)


def theme_cookie_header(value: str, secure: bool = False, max_age: int = THEME_MAX_AGE) -> tuple:
    """下发主题偏好 Cookie（仅接受 light/dark，调用方需先校验）。"""
    return _make_cookie(THEME_COOKIE, value, secure, max_age)


def parse_cookies(scope) -> dict:
    """从 ASGI scope 的 Cookie 请求头解析为 {name: value}。"""
    cookie_header = ""
    for header in scope.get("headers", []):
        if header[0] == b"cookie":
            cookie_header = header[1].decode("utf-8", errors="ignore")
            break
    cookies = SimpleCookie()
    cookies.load(cookie_header)
    return {key: morsel.value for key, morsel in cookies.items()}
