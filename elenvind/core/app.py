"""Core App：WSGI 入口与请求流水线。

    Gunicorn ──► WSGI ──► App.__call__(environ, start_response)
                            ├─ Request.from_wsgi()     解析 + 请求限制（Core）
                            ├─ bind_request()          请求级上下文（模板全局用）
                            ├─ session.load_user()     会话 -> 用户（Core）
                            ├─ Router.dispatch()       路由 + CSRF + 认证 + 权限（Core）
                            └─ send_response()         安全头 + Cookie（Core）

启动/关闭（config / logging / i18n / DB / 缓存）不由这里负责：WSGI 只有
"导入模块 + 调用 callable"两个时机，入口模块（`elenvind/wsgi.py`）在导入时
调用 `core.lifespan.startup()`，并在进程退出时调用 `shutdown()`。

`App` 刻意保持很小：它只编排，不含业务、不含 HTML。

运行期日志：每个请求在这里留下**一行**访问日志（见 `_log_access`），
它是"这个站点到底在被谁访问"的唯一来源；写事务、锁等待、业务审计
（登录/注册/改密/评论审核…）分别由 `db_base`、`session`、各业务模块记录。
所有日志都遵循同一条红线：**不记**查询串、请求体、Cookie、凭据。
"""
from __future__ import annotations

import logging
import os
import time

from .context import bind_request, reset_request
from .http import (
    HttpError,
    Request,
    error_response,
    send_early_error,
    send_response,
)
from .routing import Router
from .session import load_user

logger = logging.getLogger(__name__)

#: access 日志里路径/地址字段的最大长度（超长的 PATH_INFO 不该刷爆日志）
_ACCESS_FIELD_MAX = 200


def _log_field(value) -> str:
    """把一个请求字段变成**日志安全**文本：转义不可打印字符 + 截断。

    `PATH_INFO` 是 percent-decode 之后的：请求 `/%0aFAKE` 会得到真实换行，
    直接写进日志就等于允许客户端伪造日志行（log injection，运维会照单全收）。
    因此这里把不可打印与不可见字符统一转义成 `\\xNN` / `\\uNNNN`。
    """
    text = "" if value is None else str(value)
    out = []
    for char in text:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif 0x20 <= code < 0x7F:
            out.append(char)
        elif code < 0x100:
            out.append(f"\\x{code:02x}")
        else:
            out.append(f"\\u{code:04x}")
    safe = "".join(out)
    if len(safe) > _ACCESS_FIELD_MAX:
        return safe[:_ACCESS_FIELD_MAX] + "…"
    return safe


def _log_access(environ, request, method, status, size, started_at) -> None:
    """每个请求一行访问日志（INFO）。

    刻意**不记**查询串、请求体、Cookie、Referer、User-Agent：
    查询串可能带 token 或回跳地址，请求体/Cookie 里有密码与凭据
    （`tests/test_logging_proxy.py` 会对整条流程做脱敏断言）。
    静态资源也照记 —— 那正是访问日志的价值。
    """
    if request is not None:
        client_ip = request.client_ip
        user_id = request.user["id"] if request.user is not None else None
    else:
        # 请求对象都还没构造出来（畸形请求）：退回到 environ 里的原始信息
        client_ip = environ.get("REMOTE_ADDR")
        user_id = None
    logger.info(
        "Request handled: %s %s -> %s in %.1f ms (%s bytes, ip=%s, user=%s, pid=%s)",
        method, _log_field(environ.get("PATH_INFO")), status or "-",
        (time.perf_counter() - started_at) * 1000.0, size,
        _log_field(client_ip), user_id if user_id is not None else "-", os.getpid(),
    )


class App:
    """Elenvind 应用（WSGI callable）。

    典型的装配与运行方式（见 `elenvind/app.py`）：

        app = App(not_found=..., forbidden=..., server_error=...)
        app.router.route("/", methods=["GET"])(home)
        # gunicorn elenvind.wsgi:application

    路由由各业务模块自己的 `routes.register(router)` 装到 `app.router` 上；
    Core 不提供额外的注册层。
    """

    def __init__(self, *, router=None, not_found=None, forbidden=None,
                 server_error=None):
        self.router = router or Router()
        self.not_found = not_found
        self.forbidden = forbidden
        #: 渲染带布局的 500 页面（由装配层提供，见 `elenvind/app.py`）。
        #: 未提供或渲染失败时回落纯文本。
        #: 该回调**必须**自己保证不把异常信息写进响应体——Core 只负责调用它。
        self.server_error = server_error

    # ---------- WSGI ----------
    def __call__(self, environ, start_response):
        """处理一个 HTTP 请求；任何异常都翻译成明确的响应或 500。

        返回 WSGI 要求的"body 可迭代对象"（这里是单元素 list）。同步函数：
        内部业务逻辑、数据库访问、模板渲染全部是同步的。
        """
        #: 是否已经调用过 start_response。WSGI 里重复调用是协议错误
        #: （服务器会抛 AssertionError），因此在这里统一兜住。
        started = [False]
        sent_status = []          #: 真正发给客户端的状态码（access 日志用）
        sent_size = [0]           #: 响应体字节数（access 日志用）
        started_at = time.perf_counter()

        def guarded_start_response(status, headers, exc_info=None):
            if started[0]:
                logger.error("duplicate start_response suppressed")
                return None
            started[0] = True
            sent_status.append(str(status).split(" ", 1)[0])
            return start_response(status, headers, exc_info)

        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        head_only = method == "HEAD"
        request = None
        token = None
        try:
            try:
                request = Request.from_wsgi(environ, lang=_current_lang())
            except HttpError as error:
                # 请求本身就畸形：没有 request 对象，直接在 environ 上回错误
                body = send_early_error(guarded_start_response, environ, error)
                sent_size[0] = len(body)
                return [body]

            token = bind_request(request)
            load_user(request)

            response = self.router.dispatch(request, not_found=self.not_found,
                                            forbidden=self.forbidden)
            body = send_response(guarded_start_response, request, response,
                                 head_only=head_only)
            sent_size[0] = len(body)
            return [body]
        except Exception:
            # traceback 只进日志：logger.exception() 带完整堆栈，
            # 而客户端只会拿到错误页模板里的**固定文案**。
            logger.exception("Unhandled exception while handling %s %s",
                             environ.get("REQUEST_METHOD"),
                             environ.get("PATH_INFO"))
            if started[0]:
                return []
            body = self._server_error(guarded_start_response, environ, request,
                                      head_only=head_only)
            sent_size[0] = len(body)
            return [body]
        finally:
            # finally 里 return 表达式已经求值，因此状态码/字节数都已确定。
            # 访问日志放在 reset_request() 之前：它要用 request 上的 client_ip / user。
            _log_access(environ, request, method,
                        sent_status[0] if sent_status else None,
                        sent_size[0], started_at)
            if token is not None:
                reset_request(token)

    def _server_error(self, start_response, environ, request, *, head_only=False):
        """发送 500：优先渲染模块的错误页，任何环节失败都回落纯文本。

        三层兜底，保证"服务器出错"这个状态本身永远能回给客户端：

        1. `self.server_error(request)` —— 带布局的错误页（模块提供）；
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
                return send_early_error(start_response, environ,
                                        HttpError("Internal Server Error", 500))
            response = error_response(500)
        try:
            return send_response(start_response, request, response, head_only=head_only)
        except Exception:
            logger.exception("Failed to send the 500 response")
            raise


def _current_lang() -> str:
    """界面语言：站点级配置（config.toml locale），不做浏览器自动检测。"""
    from .config import config
    from .i18n import normalize
    return normalize(config.get("locale", "en"))
