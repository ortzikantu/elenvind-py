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
from .http import HttpError, Request, send_response
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

    def __init__(self, *, router=None, not_found=None, forbidden=None):
        self.router = router or Router()
        self.not_found = not_found
        self.forbidden = forbidden

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
            logger.exception("Unhandled exception while handling %s %s",
                             scope.get("method"), scope.get("path"))
            if response_started:
                return
            await self._send_early_error(guarded_send, scope,
                                         HttpError("Internal Server Error", 500))
        finally:
            if token is not None:
                reset_request(token)

    @staticmethod
    async def _send_early_error(send, scope, error: HttpError):
        """在请求对象还不可用时发送错误（安全头仍然照发）。"""
        from .http import BASE_SECURITY_HEADERS, HSTS_HEADER

        status = getattr(error, "status", 400)
        body = str(error.message or "Bad Request").encode("utf-8")
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        headers.extend(BASE_SECURITY_HEADERS)
        if scope.get("scheme") == "https":
            headers.append(HSTS_HEADER)
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
