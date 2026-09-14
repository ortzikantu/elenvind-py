"""ASGI HTTP 入口：请求解析、CSRF Cookie 状态、路由分发、响应统一出口。

拆分说明：
- http.py       —— 协议层 + 分发器（解析请求、组装响应、安全响应头、CSRF Cookie 下发）
- http_auth.py  —— 登录 / 注册 / 用户中心 / 登出
- http_article.py —— 文章页 GET 与评论增删恢复
- http_base.py  —— 共享类型：RequestContext / Response（http_* 模块共同依赖）
- view_*.py     —— 纯 HTML 渲染，不接触请求对象
"""
from urllib.parse import parse_qs

from .config import config
from . import view_home, view_404
from .http_base import RequestContext, html_response, plain_response, redirect_response
from .security import SESSION_COOKIE, THEME_COOKIE, parse_cookies, theme_cookie_header
from .i18n import normalize
from .db_user import get_user_by_id
from .db_session import get_session_user
from .utils import get_client_ip
from .usrpages import get_page
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

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB

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


def _safe_int(values, default):
    """容错解析查询参数中的整数，畸形输入回退默认值而非 500"""
    try:
        return int(values[0])
    except (TypeError, ValueError, IndexError):
        return default


async def _send(send, ctx, resp):
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
    await send({"type": "http.response.body", "body": resp.body})


def _theme_get(ctx):
    """GET /theme?mode=light|dark&next=/path：切换主题偏好并跳回站内页面。

    - mode 非法时不下发 Cookie（不改变现状），仅负责跳转；
    - next 必须是以单个 / 开头的站内路径（拒绝 //host 协议相对形式），防开放重定向。
    """
    mode = (ctx.query.get("mode") or [""])[0]
    next_path = (ctx.query.get("next") or ["/"])[0] or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
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
        return plain_response("Method Not Allowed", 405)

    # SEO 元文件（robots / sitemap）
    if path == "/robots.txt":
        return seo_robots_get(ctx)
    if path == "/sitemap.xml":
        return seo_sitemap_get(ctx)

    # 主题切换（GET 写偏好 Cookie，站内跳转）
    if path == "/theme":
        return _theme_get(ctx)

    # 自定义页面（usrpages/*.evmd）兜底
    slug = path.lstrip("/")
    page_html = get_page(slug)
    if page_html is not None:
        return html_response(render_custom_page(page_html=page_html, slug=slug, user=ctx.user,
                                                theme=ctx.theme, path=ctx.path, lang=ctx.lang))
    return html_response(view_404.render(user=ctx.user, theme=ctx.theme, path=ctx.path, lang=ctx.lang),
                         status=404)


def _route_post(ctx):
    path = ctx.path

    if path == "/login":
        return auth_login_post(ctx)
    if path == "/register":
        return auth_register_post(ctx)
    if path == "/user":
        return auth_user_post(ctx)
    if path == "/logout":
        return auth_logout_post(ctx)

    # 评论相关：/article/{slug}/comment | /article/{slug}/comment/{delete|restore}/{id}
    if path.startswith("/article/"):
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[2] == "comment":
            return comment_create_post(ctx, parts[1])
        if len(parts) == 5 and parts[2] == "comment":
            if parts[3] == "delete":
                return comment_delete_post(ctx, parts[1], parts[4])
            if parts[3] == "restore":
                return comment_restore_post(ctx, parts[1], parts[4])
    return plain_response("Method Not Allowed", 405)


async def _read_post_body(scope, receive, max_size):
    """读取并解析 POST 表单，返回 (状态码或 None, 表单字典)"""
    content_length = 0
    for header in scope.get("headers", []):
        if header[0] == b"content-length":
            try:
                content_length = int(header[1])
            except (TypeError, ValueError):
                return 400, {}
    if content_length > max_size:
        return 413, {}

    body = b""
    while len(body) < content_length:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, {}
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if len(body) > max_size:
                return 413, {}
            if not message.get("more_body", False):
                break

    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return 400, {}
    parsed = parse_qs(decoded)
    return None, {k: v[0] for k, v in parsed.items()}


async def handle_http(scope, receive, send):
    method = scope["method"]
    path = scope["path"]
    secure = scope.get("scheme") == "https"

    cookies = parse_cookies(scope)
    session_token = cookies.get(SESSION_COOKIE)
    user_id = get_session_user(session_token) if session_token else None
    user = get_user_by_id(user_id) if user_id else None
    client_ip = get_client_ip(scope)

    # 主题偏好：仅接受 light/dark，其余（含缺失）一律视为“跟随系统”
    theme = cookies.get(THEME_COOKIE)
    if theme not in ("light", "dark"):
        theme = None

    # 界面语言：站点级配置，由 config.toml 的 locale 字段设定（en/zh/ja）
    lang = normalize(config.get("locale", "en"))

    query = {}
    if method == "GET":
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

    if method == "GET":
        resp = _route_get(ctx)
    elif method == "POST":
        error_status, form = await _read_post_body(scope, receive, MAX_BODY_SIZE)
        if error_status is not None:
            if error_status == 413:
                resp = plain_response("Payload Too Large", 413)
            else:
                resp = plain_response("Bad Request", 400)
        else:
            ctx.form = form
            resp = _route_post(ctx)
    else:
        resp = plain_response("Method Not Allowed", 405)

    await _send(send, ctx, resp)
