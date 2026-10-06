"""Core CSRF：单点实现 + 模板 API。

模型（Double-Submit Cookie，无服务端状态，保持项目既有设计）：
- 随机令牌同时写入 Cookie（`csrf` / `__Host-csrf`，HttpOnly + SameSite=Lax）
  与表单隐藏域；
- 校验时只做两者常量时间比对；
- 跨站 POST 不会携带 SameSite=Lax 的 Cookie，因此攻击者无法同时伪造两个值。

模块只需要：
- 在模板里写 `{{ csrf_input() }}`；
- 不用关心 Cookie、令牌格式、比对方式。
Core 的调度器会**默认**对所有非安全方法（POST/PUT/PATCH/DELETE）做校验，
模块不需要（也不允许）自己写校验逻辑。
"""
import hmac
import re

from markupsafe import Markup

from .context import current_request

#: 需要 CSRF 的 HTTP 方法（RFC 7231 的"非安全方法"）
PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 令牌格式：32 字节 URL-safe 随机串固定 43 字符
_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def is_valid_token(token) -> bool:
    """校验令牌格式，避免把任意超长/异常字符串带入比较逻辑。

    用 fullmatch：`$` 在 match 语义下允许结尾多一个换行。
    """
    return isinstance(token, str) and _PATTERN.fullmatch(token) is not None


def tokens_match(submitted, expected) -> bool:
    """常量时间比对表单令牌与 Cookie 令牌。"""
    if not is_valid_token(submitted) or not is_valid_token(expected):
        return False
    return hmac.compare_digest(submitted.encode("ascii"), expected.encode("ascii"))


def validate_request(request) -> bool:
    """按当前请求对象校验 CSRF（表单字段 `csrf_token` vs Cookie）。"""
    return tokens_match(request.form.get("csrf_token", ""), request.csrf_token())


def requires_protection(method: str) -> bool:
    """该方法是否需要 CSRF 校验。"""
    return str(method).upper() in PROTECTED_METHODS


def csrf_input_html() -> Markup:
    """模板全局 `csrf_input()`：输出隐藏域。

    **必须返回 `Markup`**：Jinja 的自动转义会把普通字符串里的 `<input …>`
    转义成可见文本，导致表单里根本没有令牌（表现为所有 POST 都 403）。
    返回 Markup 表示"这是 Core 生成的可信 HTML"。

    无请求上下文时返回空串（渲染错误页/离线渲染不炸）。
    """
    from markupsafe import Markup

    from .utils import escape_html

    request = current_request()
    if request is None:
        return Markup("")
    token = request.csrf_token()
    if not token:
        return Markup("")
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape_html(token)}">')
