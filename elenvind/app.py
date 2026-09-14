import logging
from .lifespan import handle_lifespan
from .http import handle_http

logger = logging.getLogger(__name__)

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await handle_lifespan(receive, send)
    elif scope["type"] == "http":
        try:
            await handle_http(scope, receive, send)
        except Exception as e:
            logger.exception("Unhandled exception")
            # 发送 500 响应
            body = b"Internal Server Error"
            await send({
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
    else:
        raise ValueError(f"Unsupported protocol: {scope['type']}")
