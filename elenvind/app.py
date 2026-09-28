"""ASGI 应用入口：协议分发与兜底异常处理。

- lifespan：交给 lifespan.handle_lifespan（启动初始化 / 关闭清理）。
- http：交给 http.handle_http；任何未捕获异常都在这里转为 500，
  并且只在"响应尚未开始"时发送错误响应，避免二次 response.start
  触发 ASGI 协议错误（真实堆栈仍会完整写入日志）。
- 其它协议类型：明确拒绝。
"""
import logging

from .lifespan import handle_lifespan
from .http import handle_http

logger = logging.getLogger(__name__)

_ERROR_BODY = b"Internal Server Error"
_ERROR_HEADERS = [
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"content-length", str(len(_ERROR_BODY)).encode("ascii")),
    (b"cache-control", b"no-store"),
]


async def app(scope, receive, send):
    scope_type = scope.get("type")
    if scope_type == "lifespan":
        await handle_lifespan(receive, send)
    elif scope_type == "http":
        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                if response_started:
                    # 已经发过 start：丢弃重复的 start，避免协议层报错
                    logger.error("Duplicate http.response.start suppressed")
                    return
                response_started = True
            await send(message)

        try:
            await handle_http(scope, receive, guarded_send)
        except Exception:
            logger.exception("Unhandled exception while handling %s %s",
                             scope.get("method"), scope.get("path"))
            if response_started:
                # 响应已开始：无法再改写状态码，只能记录（连接由服务器收尾）
                return
            await send({
                "type": "http.response.start",
                "status": 500,
                "headers": _ERROR_HEADERS,
            })
            await send({"type": "http.response.body", "body": _ERROR_BODY})
    else:
        raise ValueError(f"Unsupported protocol: {scope_type}")
