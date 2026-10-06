"""Core HTTP 边界：Request 解析、Response 构造、Cookie 与安全响应头。

**这是全应用唯一的 HTTP 边界**。模块只消费 Request、只返回 Response
（或用 `render_template` 让 Core 包一层），不得：
- 自己解析 WSGI environ；
- 自己检查 Content-Length / Content-Type / Body 上限；
- 自己拼 Set-Cookie / Location / 安全响应头。

设计要点（保持简单，不引入中间件框架）：
- `Request.from_wsgi()` 一次性把 WSGI environ + 请求体解析成不可变语义的
  请求对象；畸形请求抛 `BadRequest` 家族异常，由调度器统一翻译成状态码。
- 表单只支持 `application/x-www-form-urlencoded`；不支持的类型抛
  `UnsupportedMediaType`，绝不猜测。
- 请求体上限、方法白名单、截断检测都在这里，只实现一次。
- 出口只有 `send_response()` / `send_early_error()`：状态行、安全头、Cookie
  都从那里出去（`wsgi_headers()` 负责把内部 bytes 头转成 WSGI 要求的 str）。

WSGI 约定（PEP 3333）：请求头来自 `environ` 的 `HTTP_*` / `CONTENT_TYPE` /
`CONTENT_LENGTH`；请求体从 `environ["wsgi.input"]` 按声明的 Content-Length
读取；响应头必须是 native `str` 且可用 latin-1 编码。
"""
from __future__ import annotations

import json
import logging
from http.client import responses as HTTP_REASONS
from urllib.parse import parse_qs

from .context import current_request
from .security import (
    CSRF_COOKIE,
    CSRF_MAX_AGE,
    PREFERENCE_COOKIES,
    SESSION_COOKIE,
    build_security_headers,
    cookie_name,
    cookie_names,
    csrf_cookie_header,
    generate_csrf_token,
    is_valid_csrf_token,
    pick_cookie,
)
from .utils import escape_html, get_client_ip, get_request_scheme, peer_address

logger = logging.getLogger(__name__)

#: 允许的请求方法；其余一律 405（带 Allow 头）
ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
#: 这些方法需要在请求对象上解析表单
_FORM_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: 表单 Content-Type
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
#: 默认请求体上限（1 MB）
DEFAULT_MAX_BODY_SIZE = 1024 * 1024
#: 单次从 `wsgi.input` 读取的字节数（单次读取上限，**不是**请求体上限）。
#: WSGI 允许输入流一次只给一部分数据（短读），因此必须循环读到声明的长度；
#: 这个值只影响循环次数，不影响能接受的最大请求体。
_BODY_READ_SIZE = 65536


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
    """一次 HTTP 请求的解析结果。由 Core 构造，模块只读消费。"""

    __slots__ = ("environ", "method", "path", "query", "headers", "cookies",
                 "raw_body", "_form", "content_type", "content_length",
                 "client_ip", "secure", "user", "session_token", "lang",
                 "_csrf", "_csrf_dirty", "_preferences",
                 "_session_cookie_dirty", "_session_cookie_clear_pending")

    def __init__(self, *, environ, method, path, query, headers, cookies, raw_body,
                 form, content_type, content_length, client_ip, secure, lang,
                 session_token=None):
        self.environ = environ
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
        #: 本次请求要写入的站内偏好（见 set_preference）
        self._preferences = {}
        #: 会话 Cookie 需要下发（新颁发/轮换）—— 普通请求不下发，见 pending_cookies
        self._session_cookie_dirty = False
        #: 会话已被本请求作废，需要在响应里清除浏览器 Cookie（见 pending_cookies）
        self._session_cookie_clear_pending = False

    # ---------- 构造 ----------
    @classmethod
    def from_wsgi(cls, environ, *, lang="en"):
        """从 WSGI environ（+ `wsgi.input`）构造 Request；畸形请求抛 HttpError。

        这是请求解析的**唯一入口**：方法白名单、请求头归一化、Cookie 解析、
        查询串解析、scheme/客户端 IP 判定、请求体读取与大小/截断校验，
        全部只在这里发生一次。
        """
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if method not in ALLOWED_METHODS:
            raise MethodNotAllowed(f"method {method} not allowed")

        headers = collect_headers(environ)
        cookies = _parse_cookie_header(headers.get("cookie", ""))
        session_token = pick_cookie(cookies, SESSION_COOKIE)
        query = parse_qs(str(environ.get("QUERY_STRING") or ""),
                         keep_blank_values=True)
        secure = get_request_scheme(environ, headers) == "https"

        raw_body = b""
        form = {}
        content_type = headers.get("content-type", "")
        content_length = None
        if method in _FORM_METHODS:
            raw_body, content_length = _read_body(environ, headers)
            if raw_body:
                mime = content_type.split(";")[0].strip().lower()
                if mime and mime != FORM_CONTENT_TYPE:
                    raise UnsupportedMediaType(
                        f"unsupported content-type: {mime}")
                form = _parse_form(raw_body)

        return cls(environ=environ, method=method, path=_request_path(environ),
                   query=query, headers=headers, cookies=cookies, raw_body=raw_body,
                   form=form, content_type=content_type,
                   content_length=content_length,
                   client_ip=get_client_ip(headers, peer_address(environ)),
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
        self._session_cookie_dirty = bool(token)

    def pending_cookies(self):
        """本次请求需要下发的全部 Cookie 头（会话 + CSRF + 站内偏好）。

        集中在一处，避免"某个分支忘了发 Cookie"这类不一致
        （例如登录成功后只设了 session 却漏了 csrf，导致下一次 POST 被 403）。

        会话 Cookie 只在**会话本身发生变化**时下发：
        - 新颁发 / 轮换（`set_session_token`）-> 下发新 token；
        - 被作废（登出 / 改密 / 删号）-> 下发清除指令；
        - 其它情况（用已有会话访问页面、取静态资源）-> **不下发**。

        最后一条很重要：静态资源与 robots/sitemap 是 `Cache-Control: public`，
        在那些响应里回带 `Set-Cookie: session=…` 会让任何"缓存 Set-Cookie"
        的中间层（CDN 缓存一切、nginx `proxy_ignore_headers Set-Cookie`）
        有机会把某人的会话 token 回放给其他访客。
        顺带也省掉了"每取一张图片就写一次 last_seen"的无谓 DB 写。
        """
        headers = []
        from .security import clear_cookie_headers, set_cookie_header
        if self.session_token and self._session_cookie_dirty:
            from .session import session_cookie_max_age
            # Cookie 寿命跟随"绝对过期"窗口（见 session_cookie_max_age）
            headers.append(set_cookie_header(self.session_token, secure=self.secure,
                                             max_age=session_cookie_max_age()))
        elif self._session_cookie_clear_pending:
            # 服务端已经作废了会话：必须让浏览器也丢掉它。
            # 用 clear_cookie_headers（同时清理可能的 __Host- 前缀变体），
            # 由 Core 统一做，模块不需要知道 Cookie 名字。
            headers.extend(clear_cookie_headers(secure=self.secure))
        csrf_header = self.csrf_cookie_header()
        if csrf_header:
            headers.append(csrf_header)
        headers.extend(self.preference_cookie_headers())
        return headers

    def invalidate_session_cookie(self):
        """作废本次请求的会话：清服务端会话 + 让浏览器 Cookie 立即过期。

        这也是模块侧"我只想让这个会话失效"的**唯一**入口 ——
        模块不拼 Cookie、不 import SESSION_COOKIE。
        """
        from .session import delete_session

        if self.session_token:
            delete_session(self.session_token)
        self.session_token = None
        self.user = None
        self._session_cookie_dirty = False
        self._session_cookie_clear_pending = True

    # ---------- 站内偏好（主题等） ----------
    def set_preference(self, name: str, value: str) -> None:
        """设置一个站内偏好（白名单内），由 Core 在响应收尾时下发 Cookie。

        模块通过它写入偏好，**不需要知道 Cookie 名字、有效期或属性**。
        为什么偏好不放 Response 上：偏好不属于某一次响应，而属于"这个浏览器"；
        统一由 `pending_cookies()` 下发，能让所有响应路径（重定向、错误页）
        行为一致。
        """
        if name not in PREFERENCE_COOKIES:
            raise ValueError(f"unknown preference {name!r}; "
                             f"expected one of {sorted(PREFERENCE_COOKIES)}")
        self._preferences[name] = value

    def preference(self, name: str, default=None):
        """读取当前请求携带的站内偏好（未设置返回 default）。"""
        from .security import pick_cookie

        if name not in PREFERENCE_COOKIES:
            raise ValueError(f"unknown preference {name!r}")
        cookie_name, _max_age, allowed = PREFERENCE_COOKIES[name]
        value = pick_cookie(self.cookies, cookie_name)
        # 白名单校验：Cookie 是客户端可控输入，绝不原样回显到 HTML/属性里
        return value if value in allowed else default

    def preference_cookie_headers(self):
        """本次需要下发的偏好 Cookie（未变更则为空）。"""
        from .security import _make_cookie

        headers = []
        for name, value in self._preferences.items():
            cookie_name, max_age, allowed = PREFERENCE_COOKIES[name]
            if value not in allowed:
                continue
            headers.append(_make_cookie(cookie_name, value, self.secure, max_age))
        return headers


def _read_body(environ, headers):
    """按 Content-Length 严格读取请求体（同步，WSGI 输入流）。

    错误语义（在 Core 里只实现一次，模块不需要关心）：
    - 缺 Content-Length（含 chunked）→ 411
    - 长度非数字 / 负数              → 400
    - 声明超过上限                  → 413（不读 body）
    - 实收少于声明（截断/断开）      → 400

    为什么是"循环读"而不是一次 `read(declared)`：PEP 3333 明确允许
    `wsgi.input` **短读**（一次只给一部分），一次 read 拿到的长度不可信。
    循环条件由**声明长度**兜底：每次迭代要么至少吃掉一个字节，
    要么立刻抛"截断" —— 既不会少读，也不可能空转（旧的异步适配层需要
    `MAX_BODY_CHUNKS` 计数器来防"永真 more_body"死循环，WSGI 下这个失败模式
    根本不存在）。
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

    if declared == 0:
        return b"", 0

    stream = environ.get("wsgi.input")
    if stream is None or not hasattr(stream, "read"):
        raise BadRequest("request body stream is unavailable")

    body = bytearray()
    while len(body) < declared:
        want = min(declared - len(body), _BODY_READ_SIZE)
        try:
            chunk = stream.read(want)
        except OSError:
            raise BadRequest("request body could not be read") from None
        if not chunk:
            # 输入流提前结束：客户端断开或截断。绝不能把"少一点"当成合法表单。
            raise BadRequest("truncated body")
        body.extend(chunk)
    return bytes(body), declared


def _request_path(environ) -> str:
    """请求路径：`SCRIPT_NAME + PATH_INFO`（PEP 3333 的完整路径）。

    gunicorn 默认 `SCRIPT_NAME=""`、`PATH_INFO` 就是完整路径；若部署方要求
    应用挂在某个前缀下（`SCRIPT_NAME` 非空），拼接后才与路由表一致。
    两者都缺失时回落 `"/"`（而不是空串，路由表以 `/` 为根）。
    """
    script = str(environ.get("SCRIPT_NAME") or "")
    path = str(environ.get("PATH_INFO") or "")
    full = script + path
    return full or "/"


def status_line(status: int) -> str:
    """WSGI 状态行（`"404 Not Found"`）：必须有原因短语。

    `http.client.responses` 是标准库里的权威表；未知状态码只发数字
    （WSGI 允许只有数字的状态行，不编造原因短语）。
    """
    code = int(status)
    reason = HTTP_REASONS.get(code, "")
    return f"{code} {reason}".rstrip()


def wsgi_headers(headers):
    """把内部 `(bytes, bytes)` 响应头转成 WSGI 要求的 native `str`。

    内部统一用 bytes（latin-1）组装，是为了让所有头（含 Cookie）走同一条
    拼装路径；这里只做一次解码，**不改变任何字节内容**。
    """
    return [(name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers]


def collect_headers(environ) -> dict:
    """请求头归一化为 {lower_name: value}。

    - `HTTP_*` → 去掉前缀、下划线换连字符（`HTTP_X_FORWARDED_FOR` →
      `x-forwarded-for`）；
    - `CONTENT_TYPE` / `CONTENT_LENGTH` 是 WSGI 的**独立**键（不带 `HTTP_`
      前缀），必须单独取，否则表单校验拿不到它们。

    同名重复头：WSGI 服务器通常会合并成一个值（gunicorn 用 `,` 连接），
    因此这里天然是"最后一次赋值生效"；应用不多做猜测。
    """
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").lower()
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            name = key.replace("_", "-").lower()
        else:
            continue
        if value is None:
            continue
        headers[name] = str(value)
    return headers


def _parse_cookie_header(raw: str) -> dict:
    """按 RFC 6265 宽松解析 Cookie；一个畸形片段不该丢掉整条头。

    实现委托 `core.security.parse_cookie_header` —— Cookie 解析只有那一处。
    （曾经 http 与 security 各有一份几乎相同的解析器，其中一份是死代码；
    两份对"多个 cookie 头"的处理**已经不同**，迟早有人按"另一份"的语义
    改坏其中一处。现在只剩这一条路径：`collect_headers()` 收集到的那个
    `cookie` 值直接交给唯一的解析器。）
    """
    from .security import parse_cookie_header

    return parse_cookie_header(raw)


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
    """HTTP 响应。模块只关心 status / body / headers；Cookie 用专门 API。"""

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
    """JSON 响应（本题不需要 API，但保持边界完整，避免模块自己拼 json）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(body, status=status, content_type="application/json; charset=utf-8",
                    headers=headers, cache_control=cache_control)


def redirect(location: str, *, status=302, headers=None) -> Response:
    """重定向。

    Location 必须由调用方给出**站内路径**。这里做两层防护：
    1. 去掉 CR/LF（响应头注入原料）；
    2. 若结果里仍有**任何控制字符**，整个 Location 直接丢弃
       （宁可让响应没有 Location，也不放出一个浏览器会自行"剥离控制字符后"
       再解析的地址 —— 那正是 `/\t/evil.com` 变 `//evil.com` 的成因）。

    用户可控的回跳地址请先过 `safe_next_path()`，不要把原始输入直接传进来。
    """
    safe = _sanitize_location(location)
    if safe and _has_url_control_chars(safe):
        logger.warning("refusing to emit a Location containing control characters")
        safe = ""
    extra = [(b"location", safe.encode("latin-1", "replace"))] if safe else []
    return Response(b"", status=status, content_type="text/plain; charset=utf-8",
                    headers=(headers or []) + extra)


def _sanitize_location(location: str) -> str:
    """去掉 CR/LF（响应头注入原料）；空值表示不带 Location。"""
    if not location:
        return ""
    return location.replace("\r", "").replace("\n", "")


#: 视为"会改变 URL 解析结果"的控制字符：C0（含 TAB/CR/LF）、DEL、C1。
#: 为什么不能只拒绝 CR/LF：
#:   浏览器在解析 URL 前会先**剥离** ASCII TAB 与换行（URL 标准），
#:   于是 `/\t/evil.com` 在浏览器眼里就是 `//evil.com`（协议相对 = 跨站）。
#:   反斜杠同理：URL 标准把它归一化成 `/`，所以 `/\evil.com` 也是跨站。
#: 注意这里用 `unicodedata` 之外的白名单并集：允许所有可打印字符，
#:   但**显式排除控制类**，避免将来又冒出一个"某个 unicode 空白被剥离"的变体。
def _has_url_control_chars(value: str) -> bool:
    import unicodedata

    for ch in value:
        if ch in "\t\r\n" or "\x00" <= ch <= "\x1f" or ch == "\x7f":
            return True
        if "\x80" <= ch <= "\x9f":
            return True
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            return True
    return False


def safe_next_path(raw, default: str = "/") -> str:
    """把"回跳地址"规范化为**站内路径**；不合法一律返回 `default`。

    这是全项目唯一的回跳地址校验（模块不得自己实现一份）。
    拒绝：
      - 非字符串 / 空串；
      - 任何控制字符（含 TAB —— 见 `_has_url_control_chars` 的说明）；
      - 反斜杠（浏览器会归一化成 `/`，`/\\evil.com` -> `//evil.com`）；
      - 不以 `/` 开头（相对路径、绝对 URL、`javascript:` 等）；
      - 以 `//` 开头（协议相对 URL = 跨站）。
    返回值一定是可以安全放进 `Location` 的站内路径。
    """
    if not isinstance(raw, str):
        return default
    value = raw.strip()
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or _has_url_control_chars(value):
        return default
    return value


# ======================= 安全响应头与最终发送 =======================

# 安全头的**定义**在 security.py（全项目唯一处）；http.py 只负责装配到响应上。


def default_cache_control(request: Request, response: Response) -> str:
    """默认缓存策略：登录态页面 no-store，匿名 HTML no-cache，其余 no-store。

    带用户状态的页面绝不允许 public——否则会被中间缓存泄漏给他人。

    **错误响应一律 no-store**：错误页代表"这次请求失败了"，没有复用价值，
    而且被缓存下来还会让访客在服务恢复后仍然看到旧的报错。
    """
    if response.status >= 400:
        return "no-store"
    if response.is_html():
        return "no-store" if request.user else "no-cache"
    return "no-store"


def build_headers(request: Request, response: Response, *, head_only=False):
    """把 Response 组装成完整的响应头（bytes；安全头 + Cookie 统一在此加）。

    **这是安全响应头的唯一注入点**：模块返回的 Response 都会经过这里，
    因此它们不需要（也不应该）自己加任何安全头。CSP / Permissions-Policy /
    HSTS 的具体取值来自配置，见 `core/security.build_security_headers()`。
    """
    headers = [
        (b"content-type", response.content_type.encode("utf-8")),
        (b"content-length", str(len(response.body)).encode("ascii")),
    ]
    headers.extend(build_security_headers(secure=request.is_secure()))

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
    """`Response.set_cookie()` 排队的 Cookie 的构造入口。

    **构造实现委托 `core.security._make_cookie`**（全项目唯一的 Set-Cookie
    拼装点，HttpOnly / SameSite=Lax / Path=/ / 可选 Secure 的策略都在那里）。
    这里曾经是一份逐行重复的拷贝 —— 两份策略迟早会各自漂移，
    例如有人只给其中一处加上 `__Host-` 要求的新属性。
    """
    from .security import _make_cookie

    return _make_cookie(name, value, secure, int(max_age), http_only=http_only)


def send_response(start_response, request: Request, response: Response, *,
                  head_only=False):
    """统一出口：任何响应都经过这里（因此安全头不可能被模块漏掉）。

    同步版（WSGI）：调用 `start_response` 发出状态行与全部响应头，
    **返回响应体字节**，由调用方（`core.app.App`）包成可迭代对象交给服务器。

    注意执行顺序：先确保 CSRF 令牌已就绪，再组装 headers。
    模板里的 `{{ csrf_input() }}` 是在**渲染时**才生成令牌的，
    如果先组装 headers 再渲染，就会出现"页面里有令牌、响应却没有 Cookie"
    的不一致（下一个 POST 必然 403）。因此这里显式提前生成。

    HEAD 请求返回空 body，但保留 GET 应有的 `Content-Length`
    （RFC 9110 允许；服务器也不会把 body 发出去）。
    """
    if not head_only:
        request.csrf_token()
    headers = build_headers(request, response, head_only=head_only)
    start_response(status_line(response.status), wsgi_headers(headers))
    return b"" if head_only else response.body


def send_early_error(start_response, environ, error: HttpError):
    """在请求对象还不可用时发送错误（安全头仍然照发）。

    请求对象还没构造出来（例如 Host 头非法、请求体超限），因此这里只能
    自己从 environ 取 scheme 判断是否 HTTPS。安全头仍走
    `build_security_headers()` 这**同一个**入口，保证早期错误响应与正常
    响应的头完全一致（验收要求 404/500 也带这些头）。

    返回响应体字节（与 `send_response` 一致，由 `App` 包成 iterable）。
    """
    status = getattr(error, "status", 400)
    body = str(error.message or "Bad Request").encode("utf-8")
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    secure = get_request_scheme(environ, collect_headers(environ)) == "https"
    headers.extend(build_security_headers(secure=secure))
    headers.append((b"cache-control", b"no-store"))
    if status in (400, 411, 413, 415):
        # 请求体可能没被完整消费。
        #
        # 注：PEP 3333 把 `Connection` 归为逐跳头，服务器可能自行决定是否
        # 发出它（gunicorn 会忽略应用给的这个头，并在 sync worker 下总是
        # 关闭连接）。保留它是因为"应用认为这次请求应当结束连接"这个意图
        # 必须留在响应里，而不是靠服务器的默认行为。
        headers.append((b"connection", b"close"))
    start_response(status_line(status), wsgi_headers(headers))
    return body


def error_response(status: int, message: str = "") -> Response:
    """统一错误响应（纯文本；页面级错误页由模块渲染带布局的 HTML）。"""
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
    # 注意：安全头的**定义**在 core.security（BASE_SECURITY_HEADERS 也在那里），
    # 本模块只负责把它们装到响应上，因此不再从这里 re-export。
    "build_security_headers",
    "build_headers", "send_response", "send_early_error", "error_response",
    "max_body_size", "collect_headers", "status_line", "wsgi_headers",
    "escape_html", "current_request", "cookie_name", "SESSION_COOKIE",
    "CSRF_MAX_AGE",
]
