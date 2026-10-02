"""Core App：ASGI 入口与请求流水线。

    ASGI ──► App.__call__
              ├─ lifespan: 启动/关闭（config / logging / i18n / DB / 缓存）
              ├─ Request.from_asgi()      解析 + 请求限制（Core）
              ├─ bind_request()           请求级上下文（模板全局用）
              ├─ session.load_user()     会话 -> 用户（Core）
              ├─ Router.dispatch()       路由 + CSRF + 认证 + 权限（Core）
              └─ send_response()         安全头 + Cookie（Core）

`App` 刻意保持很小：它只编排，不含业务、不含 HTML。
"""
from __future__ import annotations

import logging

from .context import bind_request, reset_request
from .http import HttpError, Request, error_response, send_response
from .routing import Router
from .session import load_user

logger = logging.getLogger(__name__)


class App:
    """Elenvind 应用。

    典型用法（见 `elenvind/app.py`）：

        app = App()
        app.router.route("/", methods=["GET"])(home)
        asgi = app.asgi
    """

    def __init__(self, *, router=None, not_found=None, forbidden=None,
                 server_error=None):
        self.router = router or Router()
        self.not_found = not_found
        self.forbidden = forbidden
        #: 渲染带布局的 500 页面（Feature 提供）。未提供或渲染失败时回落纯文本。
        #: 该回调**必须**自己保证不把异常信息写进响应体——Core 只负责调用它。
        self.server_error = server_error

    # ---------- 路由声明（Feature 通过它注册） ----------
    def route(self, path, methods=("GET",), auth="public", permission=None, name=None,
              fallback=False):
        return self.router.route(path, methods=methods, auth=auth,
                                 permission=permission, name=name, fallback=fallback)

    # ---------- ASGI ----------
    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            from .lifespan import handle_lifespan
            await handle_lifespan(receive, send)
            return
        if scope_type != "http":
            raise ValueError(f"Unsupported protocol: {scope_type}")
        await self.handle_http(scope, receive, send)

    async def handle_http(self, scope, receive, send):
        """处理一个 HTTP 请求；任何异常都翻译成明确的响应或 500。"""
        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                if response_started:
                    logger.error("duplicate http.response.start suppressed")
                    return
                response_started = True
            await send(message)

        request = None
        token = None
        try:
            method = str(scope.get("method", "GET")).upper()
            head_only = method == "HEAD"
            try:
                request = await Request.from_asgi(scope, receive,
                                                  lang=_current_lang())
            except HttpError as error:
                # 请求本身就畸形：没有 request 对象，直接在 scope 上回错误
                await self._send_early_error(guarded_send, scope, error)
                return

            token = bind_request(request)
            load_user(request)

            response = await self.router.dispatch(request, not_found=self.not_found,
                                                  forbidden=self.forbidden)
            await send_response(guarded_send, request, response, head_only=head_only)
        except Exception:
            # traceback 只进日志：logger.exception() 带完整堆栈，
            # 而客户端只会拿到错误页模板里的**固定文案**。
            logger.exception("Unhandled exception while handling %s %s",
                             scope.get("method"), scope.get("path"))
            if response_started:
                return
            await self._send_server_error(guarded_send, request, head_only=head_only)
        finally:
            if token is not None:
                reset_request(token)

    async def _send_server_error(self, send, request, *, head_only=False):
        """发送 500：优先渲染 Feature 的错误页，任何环节失败都回落纯文本。

        三层兜底，保证"服务器出错"这个状态本身永远能回给客户端：

        1. `self.server_error(request)` —— 带布局的错误页（Feature 提供）；
        2. 该回调抛异常或没配 -> 纯文本 "Internal Server Error"；
        3. 连发送都失败（例如响应已开始）-> 交给调用方处理，不再抛新异常。

        **不把异常对象传进渲染上下文**：错误页显示什么由模板决定，
        Core 不提供任何"把 message 塞进页面"的路径，从结构上杜绝泄漏。
        """
        response = None
        if request is not None and self.server_error is not None:
            try:
                response = self.server_error(request)
            except Exception:
                logger.exception("Rendering the 500 error page failed; "
                                 "falling back to plain text")
                response = None
        if response is None:
            if request is None:
                # 请求对象都还没构造出来：只能自己拼一个最小响应
                await self._send_early_error(send, {"scheme": "http"},
                                             HttpError("Internal Server Error", 500))
                return
            response = error_response(500)
        try:
            await send_response(send, request, response, head_only=head_only)
        except Exception:
            logger.exception("Failed to send the 500 response")
            raise

    @staticmethod
    async def _send_early_error(send, scope, error: HttpError):
        """在请求对象还不可用时发送错误（安全头仍然照发）。

        请求对象还没构造出来（例如 Host 头非法、请求体超限），
        因此这里只能自己取 scheme 判断是否 HTTPS。安全头仍走
        `build_security_headers()` 这**同一个**入口，保证早期错误响应
        与正常响应的头完全一致（验收要求 404/500 也带这些头）。
        """
        from .http import build_security_headers

        status = getattr(error, "status", 400)
        body = str(error.message or "Bad Request").encode("utf-8")
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        headers.extend(build_security_headers(secure=scope.get("scheme") == "https"))
        headers.append((b"cache-control", b"no-store"))
        if status in (400, 411, 413, 415):
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _current_lang() -> str:
    """界面语言：站点级配置（config.toml locale），不做浏览器自动检测。"""
    from .config import config
    from .i18n import normalize
    return normalize(config.get("locale", "en"))
