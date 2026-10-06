# site/utils.py
"""仅使用 Python 标准库的通用辅助工具函数"""
import html
from datetime import datetime, date

from .config import config

def escape_html(text):
    """转义 HTML 特殊字符，防止 XSS"""
    return html.escape(str(text), quote=True)

def normalize_email(email):
    """规范化邮箱地址：去除首尾空白并转为小写（用于注册/登录/修改邮箱）"""
    return str(email).strip().lower()

def peer_address(environ) -> str:
    """直连对端地址（WSGI 的 `REMOTE_ADDR`）；未知时返回 `"unknown"`。

    这是**应用唯一信任的**对端来源：它由 WSGI 服务器按真实 TCP 连接填入，
    客户端无法伪造（除非真的从那个地址连过来）。
    """
    if not environ:
        return "unknown"
    value = environ.get("REMOTE_ADDR")
    value = str(value).strip() if value else ""
    return value or "unknown"


def get_client_ip(headers, peer: str) -> str:
    """从请求头 + 直连对端解析客户端 IP 地址。

    安全边界：只有直连对端本身是受信代理（默认 127.0.0.1 / ::1，可用
    config.toml [server].trusted_proxies 覆盖）时才读取 X-Forwarded-For 的
    第一个地址；否则一律使用直连 IP。防止应用端口被公网直连时伪造代理头
    绕过登录限流等 IP 维度防护。
    """
    # 受信代理列表：默认回环地址；config 可配置（字符串或列表均可）
    if peer not in _trusted_proxies():
        return peer

    forwarded = str(headers.get("x-forwarded-for", "") or "")
    # 取第一个地址（通常是最原始的客户端）
    first = forwarded.split(",")[0].strip()
    return first or peer


def get_request_scheme(environ, headers) -> str:
    """请求 scheme：`"http"` 或 `"https"`。

    WSGI 服务器只报告**它自己看到的**那条连接：TLS 在 Nginx 终止时，
    gunicorn 收到的是明文，`wsgi.url_scheme` 永远是 `"http"`。
    因此当直连对端是受信代理时，采信 `X-Forwarded-Proto` 的第一个值。

    这里与 `get_client_ip()` 共用**同一份** `[server].trusted_proxies`
    白名单 —— 两份列表不可能漂移。历史上"HTTP 服务器一份、应用一份"的配置
    正是 HTTPS 下 Secure Cookie 丢失、或公网直连者伪造 scheme 的根源。
    """
    scheme = ""
    if environ:
        scheme = str(environ.get("wsgi.url_scheme", "") or "").strip().lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    if peer_address(environ) not in _trusted_proxies():
        return scheme
    forwarded = str(headers.get("x-forwarded-proto", "") or "")
    first = forwarded.split(",")[0].strip().lower()
    return first if first in ("http", "https") else scheme


# 受信代理列表缓存：配置只在启动时加载一次，无需每个请求重新解析
_TRUSTED_CACHE = None
_TRUSTED_SOURCE = object()


def _trusted_proxies() -> tuple:
    """解析受信代理配置：config.toml [server].trusted_proxies，默认仅回环地址。

    结果按配置内容缓存：配置只在启动时加载一次，无需每个请求重新解析。
    """
    global _TRUSTED_CACHE, _TRUSTED_SOURCE
    configured = config.get("server", {}).get("trusted_proxies")
    if _TRUSTED_CACHE is not None and _TRUSTED_SOURCE == configured:
        return _TRUSTED_CACHE
    if configured is None:
        resolved = ("127.0.0.1", "::1")
    elif isinstance(configured, str):
        resolved = tuple(ip.strip() for ip in configured.split(",") if ip.strip())
    else:
        resolved = tuple(str(ip).strip() for ip in configured if str(ip).strip())
    _TRUSTED_CACHE = resolved
    _TRUSTED_SOURCE = configured
    return resolved



def format_datetime(value):
    """
    将 ISO 字符串或 datetime 对象格式化为 'YYYY-MM-DD HH:MM'
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value  # 解析失败时原样返回（不属于程序缺陷）
    else:
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M")

def format_date(value):
    """
    将 ISO 字符串或 datetime 对象格式化为 'YYYY-MM-DD'
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value).date()
        except ValueError:
            return value
    else:
        return str(value)
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d")
    else:  # date 对象
        return dt.strftime("%Y-%m-%d")
