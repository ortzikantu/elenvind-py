"""ASGI HTTP 入口：请求解析、CSRF Cookie 状态、路由分发、响应统一出口。

拆分说明：
- http.py       —— 协议层 + 分发器（解析请求、组装响应、安全响应头、CSRF Cookie 下发）
- http_auth.py  —— 登录 / 注册 / 用户中心 / 登出
- http_article.py —— 文章页 GET 与评论增删恢复
- http_base.py  —— 共享类型：RequestContext / Response（http_* 模块共同依赖）
- view_*.py     —— 纯 HTML 渲染，不接触请求对象

请求体处理约定（关键安全边界）：
- 只接受 `Content-Length` 明确声明的表单体，且必须配 `Content-Type: application/x-www-form-urlencoded`；
  缺少长度（含 chunked）返回 411，类型不符返回 415，长度与实收字节不一致返回 400，
  超过上限返回 413 并主动要求关闭连接（不再继续读）。
- 不支持的请求方法返回 405 并带 Allow 头；HEAD 复用 GET 路由但不发送响应体。
"""
import logging
from urllib.parse import parse_qs

from .config import config
from . import view_home, view_404
from .http_base import RequestContext, html_response, plain_response, redirect_response
from .security import THEME_COOKIE, SESSION_COOKIE, parse_cookies, pick_cookie, theme_cookie_header
from .i18n import normalize
from .db_user import get_user_by_id
from .db_session import get_session_user
from .utils import get_client_ip
from .usrpages import get_page, validate_slug
from .view_custom_page import render as render_custom_page
from .http_auth import (
    auth_login_get, auth_login_post,
    auth_register_get, auth_register_post,
    auth_user_get, auth_user_post,
    auth_logout_post,
)
from .http_article import (
    article_get,
    comment_create_post,
    comment_delete_post,
    comment_restore_post,
)
from .http_seo import seo_robots_get, seo_sitemap_get

logger = logging.getLogger(__name__)

DEFAULT_MAX_BODY_SIZE = 1 * 1024 * 1024   # 1 MB
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

# 允许的请求方法：其余一律 405（带 Allow 头）
ALLOWED_METHODS = ("GET", "HEAD", "POST")

# 站点零 JS：script-src 'none' 直接掐断 XSS 执行链（页面自身不含任何 <script>，
# 若控制台出现 inline script 被拦的提示，通常是浏览器扩展注入的脚本被正确拦截）。
# style-src：'self' 放行本域 <link> 样式表；'unsafe-inline' 仅为 EVMD 图片方言
# （@{img,...,NN%}）figure 的动态 flex-basis 保留——该值由解析器生成（百分比 + calc
# 模板拼接，不含任何用户原始文本），其余视图内联样式已全部类化清零；
# http:/https: 允许静态资源由独立静态服务器/Nginx/CDN 域名提供（与 img/media 策略一致）。
CSP = (
    "default-src 'none'; script-src 'none'; "
    "style-src 'self' 'unsafe-inline' http: https:; "
    "img-src 'self' data: http: https:; media-src 'self' http: https:; "
    "font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; manifest-src 'none'"
)

BASE_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"content-security-policy", CSP.encode("utf-8")),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]
HSTS_HEADER = (b"strict-transport-security", b"max-age=31536000")

# 每个连接最多接收多少个 ASGI body 分片：防止"永不停歇的 more_body"造成死循环
MAX_BODY_CHUNKS = 1024


def max_body_size() -> int:
    """请求体上限（config.toml 顶层 max_body_size，默认 1 MB）。"""
    value = config.get("max_body_size", DEFAULT_MAX_BODY_SIZE)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_MAX_BODY_SIZE
    return value


def _safe_int(values, default):
    """容错解析查询参数中的整数，畸形输入回退默认值而非 500"""
    try:
        return int(values[0])
    except (TypeError, ValueError, IndexError):
        return default


def _header_value(scope, name: bytes):
    """取请求头（同名取最后一个），不存在返回 None。"""
    value = None
    for header_name, header_value in scope.get("headers", []):
        if header_name == name:
            value = header_value
    return value


async def _send(send, ctx, resp, *, head_only: bool = False):
    headers = [
        (b"content-type", resp.content_type.encode("utf-8")),
        (b"content-length", str(len(resp.body)).encode("utf-8")),
    ]
    headers.extend(BASE_SECURITY_HEADERS)
    if ctx.secure:
        headers.append(HSTS_HEADER)
    # 缓存策略：登录用户的页面含个性化内容，禁止缓存；匿名页允许按需重新验证。
    # 若响应自身携带 cache-control（如 SEO 元文件），则尊重自定义值，不再叠加默认头
    if not any(name == b"cache-control" for name, _ in resp.headers):
        if resp.content_type.startswith("text/html"):
            headers.append((b"cache-control", b"no-store" if ctx.user else b"no-cache"))
        else:
            headers.append((b"cache-control", b"no-store"))
    headers.extend(resp.headers)
    csrf_header = ctx.take_csrf_cookie_header()
    if csrf_header:
        headers.append(csrf_header)

    await send({
        "type": "http.response.start",
        "status": resp.status,
        "headers": headers,
    })
    # HEAD 必须与 GET 同头不同体
    await send({"type": "http.response.body", "body": b"" if head_only else resp.body})


def _theme_get(ctx):
    """GET /theme?mode=light|dark&next=/path：切换主题偏好并跳回站内页面。

    - mode 非法时不下发 Cookie（不改变现状），仅负责跳转；
    - next 必须是以单个 / 开头的站内路径（拒绝 //host 协议相对形式），防开放重定向。
    """
    mode = (ctx.query.get("mode") or [""])[0]
    next_path = (ctx.query.get("next") or ["/"])[0] or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    # 去掉可能存在的换行/回车，避免被拼进 Location 头时造成响应头注入
    if "\r" in next_path or "\n" in next_path:
        next_path = "/"
    headers = []
    if mode in ("light", "dark"):
        headers.append(theme_cookie_header(mode, secure=ctx.secure))
    return redirect_response(next_path, headers=headers)


def _route_get(ctx):
    path = ctx.path

    # 首页（分页）
    if path == "/":
        page = _safe_int(ctx.query.get("page"), 1)
        return html_response(view_home.render(user=ctx.user, page=page, theme=ctx.theme,
                                              path=ctx.path, lang=ctx.lang))

    # 文章页
    if path.startswith("/article/"):
        return article_get(ctx, path[len("/article/"):])

    # 固定页面
    if path == "/login":
        return auth_login_get(ctx)
    if path == "/register":
        return auth_register_get(ctx)
    if path == "/user":
        return auth_user_get(ctx)
    if path == "/logout":
        # 登出是状态变更操作，只接受带 CSRF 的 POST
        return plain_response("Method Not Allowed", 405, headers=[(b"allow", b"POST")])

    # SEO 元文件（robots / sitemap）
    if path == "/robots.txt":
        return seo_robots_get(ctx)
    if path == "/sitemap.xml":
        return seo_sitemap_get(ctx)

    # 主题切换（GET 写偏好 Cookie，站内跳转）
    if path == "/theme":
        return _theme_get(ctx)

    # 自定义页面（usrpages/*.evmd）兜底：slug 必须先通过白名单校验，
    # 非法路径直接 404，绝不进入文件系统查找逻辑
    slug = path.lstrip("/")
    if validate_slug(slug):
        page_html = get_page(slug)
        if page_html is not None:
            return html_response(render_custom_page(page_html=page_html, slug=slug, user=ctx.user,
                                                    theme=ctx.theme, path=ctx.path, lang=ctx.lang))
    return html_response(view_404.render(user=ctx.user, theme=ctx.theme, path=ctx.path, lang=ctx.lang),
                         status=404)


def _route_post(ctx):
    path = ctx.path

    # 先确定路由，再校验 CSRF：未知路径应返回 405，而不是把 CSRF 结果当作路由依据
    handler = None
    if path == "/login":
        handler = auth_login_post
    elif path == "/register":
        handler = auth_register_post
    elif path == "/user":
        handler = auth_user_post
    elif path == "/logout":
        handler = auth_logout_post
    elif path.startswith("/article/"):
        # 评论相关：/article/{slug}/comment | /article/{slug}/comment/{delete|restore}/{id}
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[2] == "comment":
            handler = lambda c: comment_create_post(c, parts[1])
        elif len(parts) == 5 and parts[2] == "comment" and parts[3] == "delete":
            handler = lambda c: comment_delete_post(c, parts[1], parts[4])
        elif len(parts) == 5 and parts[2] == "comment" and parts[3] == "restore":
            handler = lambda c: comment_restore_post(c, parts[1], parts[4])

    if handler is None:
        return plain_response("Method Not Allowed", 405, headers=[(b"allow", b"GET, HEAD")])

    # CSRF 统一闸门：所有状态变更 POST 都必须通过校验（GET 永不改状态）。
    # 放在分发器里而不是各视图内，避免将来新增表单时漏检。
    if not ctx.csrf_ok():
        return plain_response("Invalid CSRF token", 400)

    return handler(ctx)


async def _read_post_body(scope, receive, max_size: int):
    """读取并解析 POST 表单，返回 (状态码或 None, 表单字典)。

    错误语义：
    - 411：缺少 Content-Length（不支持 chunked/流式请求体）
    - 400：长度非法（负数/非数字）、类型不符之外的解码错误、长度与实收不符、客户端断开
    - 413：声明或实收的 body 超过 max_size
    - 415：Content-Type 不是 application/x-www-form-urlencoded
    - 200(None)：解析成功，第二个返回值为 {"已解码表单"}
    """
    raw_length = _header_value(scope, b"content-length")
    if raw_length is None:
        # 明确拒绝不支持的 body framing，而不是静默当作空表单
        return 411, {}
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError):
        return 400, {}
    if content_length < 0:
        return 400, {}
    if content_length > max_size:
        return 413, {}

    raw_type = _header_value(scope, b"content-type")
    if raw_type is not None:
        mime = raw_type.decode("latin-1").split(";")[0].strip().lower()
        if mime and mime != FORM_CONTENT_TYPE:
            return 415, {}

    body = bytearray()
    chunks = 0
    # 必须读到声明长度才算完整；空分片不计入进度，因此额外用分片数兜底防死循环
    while len(body) < content_length:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            return 400, {}
        if message_type != "http.request":
            return 400, {}
        chunk = message.get("body", b"")
        if chunk:
            chunks += 1
            if chunks > MAX_BODY_CHUNKS:
                return 413, {}
            body.extend(chunk)
            if len(body) > max_size:
                return 413, {}
        if not message.get("more_body", False) and len(body) < content_length:
            # 客户端提前结束：截断的 body 一律拒绝，不能当合法请求处理
            return 400, {}

    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return 400, {}
    try:
        parsed = parse_qs(decoded, keep_blank_values=True)
    except ValueError:
        return 400, {}
    return None, {key: values[0] for key, values in parsed.items() if values}


def _error_response(status: int):
    """把 _read_post_body 的状态码映射为响应；请求体有问题时要求关闭连接。"""
    reason = {400: "Bad Request", 411: "Length Required",
              413: "Payload Too Large", 415: "Unsupported Media Type"}.get(status, "Bad Request")
    headers = []
    if status in (400, 413):
        # 我们对 body 的消费与客户端预期可能已经不一致，直接关闭连接最安全
        headers.append((b"connection", b"close"))
    return plain_response(reason, status, headers=headers)


async def handle_http(scope, receive, send):
    method = scope.get("method", "GET")
    path = scope.get("path", "/")
    secure = scope.get("scheme") == "https"

    cookies = parse_cookies(scope)
    session_token = pick_cookie(cookies, SESSION_COOKIE)
    user_id = get_session_user(session_token) if session_token else None
    user = get_user_by_id(user_id) if user_id else None
    client_ip = get_client_ip(scope)

    # 主题偏好：仅接受 light/dark，其余（含缺失）一律视为"跟随系统"
    theme = pick_cookie(cookies, THEME_COOKIE)
    if theme not in ("light", "dark"):
        theme = None

    # 界面语言：站点级配置，由 config.toml 的 locale 字段设定（en/zh/ja）
    lang = normalize(config.get("locale", "en"))

    query = {}
    if method in ("GET", "HEAD"):
        raw_query = scope.get("query_string", b"").decode("utf-8", errors="replace")
        query = parse_qs(raw_query, keep_blank_values=True)

    ctx = RequestContext(
        scope=scope,
        cookies=cookies,
        session_token=session_token,
        user=user,
        client_ip=client_ip,
        secure=secure,
        method=method,
        path=path,
        query=query,
        form={},
        theme=theme,
        lang=lang,
    )

    if method in ("GET", "HEAD"):
        resp = _route_get(ctx)
        await _send(send, ctx, resp, head_only=(method == "HEAD"))
        return

    if method == "POST":
        error_status, form = await _read_post_body(scope, receive, max_body_size())
        if error_status is not None:
            resp = _error_response(error_status)
        else:
            ctx.form = form
            resp = _route_post(ctx)
        await _send(send, ctx, resp)
        return

    resp = plain_response("Method Not Allowed", 405,
                          headers=[(b"allow", ", ".join(ALLOWED_METHODS).encode("ascii"))])
    await _send(send, ctx, resp)
