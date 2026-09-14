"""轻量请求上下文与响应封装：http.py 分发器与各 http_* 模块的共享类型。

设计目标：
- 上下文对象（RequestContext）在一次请求内携带已解析的 Cookie/会话/表单等状态，
  避免 handler 直接访问 ASGI scope，也避免 http.py 与 handler 模块循环依赖。
- CSRF 采用无状态 Double-Submit Cookie：随机令牌同时写入 Cookie 与表单。
  上下文负责"读取已有令牌 / 必要时生成新令牌"，并由分发器统一在响应中附加 Set-Cookie。
"""
from .security import (
    CSRF_COOKIE,
    generate_csrf_token,
    is_valid_csrf_token,
    csrf_cookie_header,
)

class Response:
    __slots__ = ("status", "content_type", "body", "headers")

    def __init__(self, status: int, content_type: str, body: bytes, headers=None):
        self.status = status
        self.content_type = content_type
        self.body = body
        self.headers = headers or []

def html_response(body: str, status: int = 200, headers=None) -> Response:
    return Response(status, "text/html; charset=utf-8", body.encode("utf-8"), headers)

def plain_response(text: str, status: int = 200,
                   content_type: str = "text/plain; charset=utf-8", headers=None) -> Response:
    return Response(status, content_type, text.encode("utf-8"), headers)

def redirect_response(location: str, headers=None) -> Response:
    extra = [(b"location", location.encode("utf-8"))] if location else []
    return Response(302, "text/plain; charset=utf-8", b"", (headers or []) + extra)

class RequestContext:
    """单次 HTTP 请求的解析结果与状态。由 http.py 构建，handler 只读使用。"""

    __slots__ = (
        "scope", "cookies", "session_token", "user", "client_ip", "secure",
        "method", "path", "query", "form", "theme", "lang", "_csrf", "_csrf_dirty",
    )

    def __init__(self, scope, cookies, session_token, user, client_ip,
                 secure, method, path, query, form, theme=None, lang="en"):
        self.scope = scope
        self.cookies = cookies
        self.session_token = session_token
        self.user = user
        self.client_ip = client_ip
        self.secure = secure          # 请求是否为 HTTPS（决定 Cookie Secure 标记）
        self.method = method
        self.path = path
        self.query = query            # GET 查询参数（list 值，与 parse_qs 一致）
        self.form = form              # POST 表单（已取首个值）
        self.theme = theme            # 手动主题偏好："light"/"dark"，未设置时 None（跟随系统）
        self.lang = lang              # 界面语言：en/zh/ja（Cookie -> Accept-Language -> 配置）
        self._csrf = None
        self._csrf_dirty = False

    def csrf(self):
        """返回 Cookie 中已有的合法 CSRF 令牌；不存在或格式非法返回 None"""
        if self._csrf is None:
            token = self.cookies.get(CSRF_COOKIE)
            self._csrf = token if is_valid_csrf_token(token) else None
        return self._csrf

    def ensure_csrf(self):
        """返回可嵌入表单的 CSRF 令牌；缺失时生成新令牌并标记需写入 Cookie"""
        token = self.csrf()
        if token is None:
            token = generate_csrf_token()
            self._csrf = token
            self._csrf_dirty = True
        return token

    def take_csrf_cookie_header(self):
        """若本请求生成了新令牌，返回对应的 Set-Cookie 头（仅一次，供分发器附加）"""
        if self._csrf_dirty:
            self._csrf_dirty = False
            return csrf_cookie_header(self._csrf, secure=self.secure)
        return None
