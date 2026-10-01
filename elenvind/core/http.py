"""Core HTTP 边界：Request 解析、Response 构造、Cookie 与安全响应头。

**这是全应用唯一的 HTTP 边界**。Feature 只消费 Request、只返回 Response
（或用 `render_template` 让 Core 包一层），不得：
- 自己解析 ASGI scope；
- 自己检查 Content-Length / Content-Type / Body 上限；
- 自己拼 Set-Cookie / Location / 安全响应头。

设计要点（保持简单，不引入中间件框架）：
- `Request.from_asgi()` 一次性把 scope + body 解析成不可变语义的请求对象；
  畸形请求抛 `BadRequest` 家族异常，由调度器统一翻译成状态码。
- 表单只支持 `application/x-www-form-urlencoded`；不支持的类型抛
  `UnsupportedMediaType`，绝不猜测。
- 请求体上限、方法白名单、截断检测都在这里，只实现一次。
"""
from __future__ import annotations

import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from .context import current_request
from .security import (
    BASE_SECURITY_HEADERS,
    CSP,
    CSRF_COOKIE,
    CSRF_MAX_AGE,
    HSTS_HEADER,
    SESSION_COOKIE,
    cookie_name,
    cookie_names,
    csrf_cookie_header,
    generate_csrf_token,
    is_valid_csrf_token,
    pick_cookie,
)
from .utils import escape_html, get_client_ip

#: 允许的请求方法；其余一律 405（带 Allow 头）
ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
#: 这些方法需要在请求对象上解析表单
_FORM_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: 表单 Content-Type
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
#: 默认请求体上限（1 MB）
DEFAULT_MAX_BODY_SIZE = 1024 * 1024
#: 单个请求最多接收多少个 ASGI body 分片（防"永真 more_body"死循环）
MAX_BODY_CHUNKS = 1024


# ======================= 异常：由调度器翻译成状态码 =======================

class HttpError(Exception):
    """HTTP 边界错误：携带应返回的状态码与简短原因。"""

    status = 400

    def __init__(self, message="Bad Request", status=None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class BadRequest(HttpError):
    status = 400


class LengthRequired(HttpError):
    status = 411


class PayloadTooLarge(HttpError):
    status = 413


class UnsupportedMediaType(HttpError):
    status = 415


class MethodNotAllowed(HttpError):
    status = 405


class Forbidden(HttpError):
    status = 403


class NotFound(HttpError):
    status = 404


class CsrfError(Forbidden):
    """CSRF 校验失败：统一 403（语义上属于"拒绝"而不是"格式错误"）。"""

    def __init__(self, message="CSRF validation failed"):
        super().__init__(message, status=403)


# ======================= Request =======================

class Request:
    """一次 HTTP 请求的解析结果。由 Core 构造，Feature 只读消费。"""

    __slots__ = ("scope", "method", "path", "query", "headers", "cookies",
                 "raw_body", "_form", "content_type", "content_length",
                 "client_ip", "secure", "user", "session_token", "lang",
                 "_csrf", "_csrf_dirty")

    def __init__(self, *, scope, method, path, query, headers, cookies, raw_body,
                 form, content_type, content_length, client_ip, secure, lang,
                 session_token=None):
        self.scope = scope
        self.method = method
        self.path = path
        self.query = query                # {key: [values]}
        self.headers = headers            # {lower-name: value}
        self.cookies = cookies            # {name: value}
        self.raw_body = raw_body
        self._form = form                 # {key: value}（已取首个值）
        self.content_type = content_type
        self.content_length = content_length
        self.client_ip = client_ip
        self.secure = secure
        self.lang = lang
        self.user = None                  # 由 session 层填充
        self.session_token = session_token  # 从 Cookie 解析（可能为 None）
        self._csrf = None
        self._csrf_dirty = False

    # ---------- 构造 ----------
    @classmethod
    async def from_asgi(cls, scope, receive, *, lang="en"):
        """从 ASGI scope/receive 构造 Request；畸形请求抛 HttpError。"""
        method = str(scope.get("method", "GET")).upper()
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(f"method {method} not allowed")

        headers = _collect_headers(scope)
        cookies = _parse_cookie_header(headers.get("cookie", ""))
        session_token = pick_cookie(cookies, SESSION_COOKIE)
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"),
                         keep_blank_values=True)
        secure = scope.get("scheme") == "https"

        raw_body = b""
        form = {}
        content_type = headers.get("content-type", "")
        content_length = None
        if method in _FORM_METHODS:
            raw_body, content_length = await _read_body(scope, receive, headers)
            if raw_body:
                mime = content_type.split(";")[0].strip().lower()
                if mime and mime != FORM_CONTENT_TYPE:
                    raise UnsupportedMediaType(
                        f"unsupported content-type: {mime}")
                form = _parse_form(raw_body)

        return cls(scope=scope, method=method, path=str(scope.get("path", "/")),
                   query=query, headers=headers, cookies=cookies, raw_body=raw_body,
                   form=form, content_type=content_type,
                   content_length=content_length, client_ip=get_client_ip(scope),
                   secure=secure, lang=lang, session_token=session_token)

    # ---------- 便捷访问 ----------
    @property
    def form(self) -> dict:
        return self._form

    def arg(self, name, default=None):
        """取查询参数首个值。"""
        values = self.query.get(name)
        return values[0] if values else default

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def is_secure(self) -> bool:
        return bool(self.secure)

    # ---------- CSRF ----------
    def csrf_token(self) -> str:
        """当前请求的 CSRF 令牌：Cookie 里合法就用它，否则生成新的并排队下发。"""
        if self._csrf is None:
            existing = pick_cookie(self.cookies, CSRF_COOKIE)
            if is_valid_csrf_token(existing):
                self._csrf = existing
            else:
                self._csrf = generate_csrf_token()
                self._csrf_dirty = True
        return self._csrf

    def csrf_cookie_header(self):
        """需要下发新 CSRF Cookie 时返回 Set-Cookie 头，否则 None。"""
        if self._csrf_dirty:
            self._csrf_dirty = False
            return csrf_cookie_header(self._csrf, secure=self.secure, base=CSRF_COOKIE)
        return None

    # ---------- Session ----------
    def set_session_token(self, token) -> None:
        """Core 会话层在轮换/颁发会话后调用（Cookie 由 send_response 统一下发）。"""
        self.session_token = token

    def pending_cookies(self):
        """本次请求需要下发的全部 Cookie 头（会话 + CSRF）。

        集中在一处，避免"某个分支忘了发 Cookie"这类不一致
        （例如登录成功后只设了 session 却漏了 csrf，导致下一次 POST 被 403）。
        """
        headers = []
        if self.session_token:
            from .security import set_cookie_header
            headers.append(set_cookie_header(self.session_token, secure=self.secure))
        csrf_header = self.csrf_cookie_header()
        if csrf_header:
            headers.append(csrf_header)
        return headers


async def _read_body(scope, receive, headers):
    """按 Content-Length 严格读取请求体。

    错误语义（在 Core 里只实现一次，Feature 不需要关心）：
    - 缺 Content-Length（含 chunked）→ 411
    - 长度非数字 / 负数              → 400
    - 声明或实收超过上限            → 413
    - 实收少于声明（截断/断开）      → 400
    """
    raw_length = headers.get("content-length")
    if raw_length is None:
        raise LengthRequired("Content-Length is required")
    try:
        declared = int(raw_length)
    except (TypeError, ValueError):
        raise BadRequest("invalid Content-Length") from None
    if declared < 0:
        raise BadRequest("negative Content-Length")
    limit = max_body_size()
    if declared > limit:
        raise PayloadTooLarge("declared body too large")

    body = bytearray()
    chunks = 0
    while len(body) < declared:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            raise BadRequest("client disconnected")
        if message_type != "http.request":
            raise BadRequest("unexpected ASGI message")
        chunk = message.get("body", b"")
        if chunk:
            chunks += 1
            if chunks > MAX_BODY_CHUNKS:
                raise PayloadTooLarge("too many body chunks")
            body.extend(chunk)
            if len(body) > limit:
                raise PayloadTooLarge("body too large")
        if not message.get("more_body", False) and len(body) < declared:
            raise BadRequest("truncated body")
    return bytes(body), declared


def _collect_headers(scope) -> dict:
    """请求头归一化为 {lower_name: value}（同名取最后一个）。"""
    headers = {}
    for name, value in scope.get("headers", []):
        try:
            key = name.decode("latin-1").lower()
            headers[key] = value.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):
            continue
    return headers


def _parse_cookie_header(raw: str) -> dict:
    """按 RFC 6265 宽松解析 Cookie；一个畸形片段不该丢掉整条头。

    不用 http.cookies.SimpleCookie 的原因：它遇到 `=broken` 这类片段会
    把整个头部的 Cookie 全部丢弃，导致一个无关的坏 Cookie 让所有人掉线。
    """
    cookies = {}
    for part in raw.split(";"):
        name, sep, value = part.partition("=")
        if not sep:
            continue
        name = name.strip()
        value = value.strip()
        if not name or "=" in name or value.startswith('"'):
            continue
        cookies[name] = value
    return cookies


def _parse_form(raw_body: bytes) -> dict:
    """解析 urlencoded 表单；取每个键的第一个值。"""
    try:
        decoded = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        raise BadRequest("body is not valid UTF-8") from None
    try:
        parsed = parse_qs(decoded, keep_blank_values=True)
    except ValueError:
        raise BadRequest("malformed form body") from None
    return {key: values[0] for key, values in parsed.items() if values}


def max_body_size() -> int:
    """请求体上限（config.toml 顶层 max_body_size，默认 1 MB）。"""
    from .config import config
    value = config.get("max_body_size", DEFAULT_MAX_BODY_SIZE)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_MAX_BODY_SIZE
    return value


# ======================= Response =======================

class Response:
    """HTTP 响应。Feature 只关心 status / body / headers；Cookie 用专门 API。"""

    __slots__ = ("status", "body", "content_type", "headers", "cookies",
                 "cache_control")

    def __init__(self, body=b"", *, status=200,
                 content_type="text/html; charset=utf-8", headers=None,
                 cache_control=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = list(headers or [])
        self.cookies = []          # [(base_name, value, max_age, http_only)]
        self.cache_control = cache_control

    # ---------- 构造 helper ----------
    def set_cookie(self, name, value, *, max_age, http_only=True):
        """排队一个 Set-Cookie（真正的头由 Core 统一拼装）。"""
        self.cookies.append((name, value, max_age, http_only))
        return self

    def delete_cookie(self, name):
        """让 Cookie 立即过期（含前缀兼容名）。"""
        for candidate in cookie_names(name):
            self.cookies.append((candidate, "", 0, True))
        return self

    def with_cache_control(self, value):
        self.cache_control = value
        return self

    def is_html(self) -> bool:
        return self.content_type.startswith("text/html")


def html(body, *, status=200, headers=None, cache_control=None) -> Response:
    """HTML 响应。body 可以是 str 或 Markup。"""
    return Response(body, status=status, content_type="text/html; charset=utf-8",
                    headers=headers, cache_control=cache_control)


def text(body, *, status=200, content_type="text/plain; charset=utf-8",
         headers=None, cache_control=None) -> Response:
    return Response(body, status=status, content_type=content_type,
                    headers=headers, cache_control=cache_control)


def json_response(payload, *, status=200, headers=None, cache_control=None) -> Response:
    """JSON 响应（本题不需要 API，但保持边界完整，避免 Feature 自己拼 json）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(body, status=status, content_type="application/json; charset=utf-8",
                    headers=headers, cache_control=cache_control)


def redirect(location: str, *, status=302, headers=None) -> Response:
    """重定向。Location 必须由调用方给出站内路径（Core 会去掉 CR/LF）。"""
    safe = _sanitize_location(location)
    extra = [(b"location", safe.encode("latin-1", "replace"))] if safe else []
    return Response(b"", status=status, content_type="text/plain; charset=utf-8",
                    headers=(headers or []) + extra)


def _sanitize_location(location: str) -> str:
    """去掉 CR/LF（响应头注入原料）；空值表示不带 Location。"""
    if not location:
        return ""
    return location.replace("\r", "").replace("\n", "")


# ======================= 安全响应头与最终发送 =======================

# 安全头的**定义**在 security.py（全项目唯一处）；http.py 只负责装配到响应上。


def default_cache_control(request: Request, response: Response) -> str:
    """默认缓存策略：登录态页面 no-store，匿名 HTML no-cache，其余 no-store。

    带用户状态的页面绝不允许 public——否则会被中间缓存泄漏给他人。
    """
    if response.is_html():
        return "no-store" if request.user else "no-cache"
    return "no-store"


def build_headers(request: Request, response: Response, *, head_only=False):
    """把 Response 组装成完整的 ASGI 响应头（安全头 + Cookie 统一在此加）。"""
    headers = [
        (b"content-type", response.content_type.encode("utf-8")),
        (b"content-length", str(len(response.body)).encode("ascii")),
    ]
    headers.extend(BASE_SECURITY_HEADERS)
    if request.is_secure():
        headers.append(HSTS_HEADER)

    cache_control = response.cache_control or default_cache_control(request, response)
    if cache_control:
        headers.append((b"cache-control", cache_control.encode("latin-1")))

    for name, value, max_age, http_only in response.cookies:
        headers.append(_set_cookie_header(name, value, max_age=max_age,
                                          http_only=http_only, secure=request.is_secure()))
    headers.extend(request.pending_cookies())
    headers.extend(response.headers)
    return headers


def _set_cookie_header(name, value, *, max_age, http_only, secure):
    """唯一的 Set-Cookie 构造点（HttpOnly / SameSite=Lax / Path=/ / 可选 Secure）。"""
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    if http_only:
        cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Lax"
    cookie[name]["max-age"] = int(max_age)
    if secure:
        cookie[name]["secure"] = True
    return (b"set-cookie", cookie[name].OutputString().encode("utf-8"))


async def send_response(send, request: Request, response: Response, *, head_only=False):
    """统一出口：任何响应都经过这里（因此安全头不可能被 Feature 漏掉）。

    注意执行顺序：先确保 CSRF 令牌已就绪，再组装 headers。
    模板里的 `{{ csrf_input() }}` 是在**渲染时**才生成令牌的，
    如果先组装 headers 再渲染，就会出现"页面里有令牌、响应却没有 Cookie"
    的不一致（下一个 POST 必然 403）。因此这里显式提前生成。
    """
    if not head_only:
        request.csrf_token()
    headers = build_headers(request, response, head_only=head_only)
    await send({
        "type": "http.response.start",
        "status": response.status,
        "headers": headers,
    })
    await send({"type": "http.response.body",
                "body": b"" if head_only else response.body})


def error_response(status: int, message: str = "") -> Response:
    """统一错误响应（纯文本；页面级错误页由 Feature 渲染带布局的 HTML）。"""
    reasons = {400: "Bad Request", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 411: "Length Required",
               413: "Payload Too Large", 415: "Unsupported Media Type",
               500: "Internal Server Error"}
    body = message or reasons.get(status, "Error")
    headers = []
    if status in (400, 411, 413, 415):
        # 请求体可能没被完整消费，直接关闭连接最安全
        headers.append((b"connection", b"close"))
    return text(body, status=status, headers=headers)


__all__ = [
    "ALLOWED_METHODS", "FORM_CONTENT_TYPE", "DEFAULT_MAX_BODY_SIZE",
    "HttpError", "BadRequest", "LengthRequired", "PayloadTooLarge",
    "UnsupportedMediaType", "MethodNotAllowed", "Forbidden", "NotFound", "CsrfError",
    "Request", "Response", "html", "text", "json_response", "redirect",
    "BASE_SECURITY_HEADERS", "HSTS_HEADER", "CSP",
    "build_headers", "send_response", "error_response", "max_body_size",
    "escape_html", "current_request", "cookie_name", "SESSION_COOKIE", "CSRF_MAX_AGE",
]
