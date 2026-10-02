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

def get_client_ip(scope):
    """
    从 ASGI scope 中提取客户端 IP 地址。

    安全边界：只有直连对端本身是受信代理（默认 127.0.0.1 / ::1，可用
    config.toml [server].trusted_proxies 覆盖）时才读取 X-Forwarded-For 的
    第一个地址；否则一律使用直连 IP。防止应用端口被公网直连时伪造代理头
    绕过登录限流等 IP 维度防护。
    """
    client = scope.get("client")
    peer_ip = client[0] if client else "unknown"

    # 受信代理列表：默认回环地址；config 可配置（字符串或列表均可）
    trusted = _trusted_proxies()
    if peer_ip not in trusted:
        return peer_ip

    for header_name, header_value in scope.get("headers", []):
        if header_name == b"x-forwarded-for":
            # 取第一个 IP（通常是最原始的客户端）
            forwarded = header_value.decode("latin-1").split(",")[0].strip()
            if forwarded:
                return forwarded
    return peer_ip


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
