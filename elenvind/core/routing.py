"""Core Routing：`@route` 声明 + 单一调度器。

目标是"声明即安全"：

    @route("/settings", methods=["GET", "POST"], auth="required")
    def settings(request):
        return render_template("settings.html", {...})

未声明的安全能力由 Core **默认施加**，Feature 不写也不会漏：

| 能力 | 由谁施加 | 触发条件 |
|---|---|---|
| CSRF 校验 | 调度器 | 方法属于 POST/PUT/PATCH/DELETE |
| 认证 | 调度器 | `auth="required"` |
| 权限 | 调度器 | `permission="admin"` |
| 安全响应头 | `send_response` | 所有响应 |
| Cookie 属性 | `http._set_cookie_header` | 所有 Set-Cookie |
| 请求体上限 / Content-Type / framing | `Request.from_asgi` | 所有请求 |

设计取舍（刻意不做的事）：没有中间件栈、没有依赖注入、没有插件、
没有 blueprints。路由表就是一个 list[(path, methods, handler, spec)]，
路径匹配按段比较，动态段用 `<name>` 语法。
"""
from __future__ import annotations

from .auth import AUTHENTICATED, PUBLIC, check_permission
from .csrf import requires_protection, validate_request
from .http import HttpError, error_response, redirect

#: 登录页路径（认证闸门的重定向目标）
LOGIN_PATH = "/login"
#: 站内跳转参数名（登录后回到原页面）
NEXT_PARAM = "next"


def _login_redirect(request):
    """未登录访问受保护页面时的友好处理：跳登录页并记住原目标。

    用户体验优先：直接 403 会让人一头雾水；跳登录页并带回跳地址才是预期行为。
    `next` 只接受**站内路径**（防开放重定向），登录后由 auth Feature 消费。
    """
    from urllib.parse import quote

    target = request.path or "/"
    if request.query:
        pairs = []
        for key, values in request.query.items():
            for value in values:
                pairs.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
        if pairs:
            target = f"{target}?{'&'.join(pairs)}"
    return redirect(f"{LOGIN_PATH}?{NEXT_PARAM}={quote(target, safe='')}")

#: `auth=` 声明为这些值之一时，要求已登录。
#: `"required"` 是路由里实际使用的写法；`"authenticated"` 作为同义词一并接受，
#: 避免"声明了但 Gate 不认识"这种**失效开放**的隐患。
AUTH_REQUIRED_VALUES = frozenset({AUTHENTICATED, "required"})


class RouteMiss(Exception):
    """路由"路径匹配但资源不存在"，请尝试下一个候选路由。

    只有明确知道自己只是**某些**路径的处理器时才该抛它。典型场景是静态文件：
    `/<path:x>` 匹配所有路径，但只有磁盘上真有文件时才算命中；
    文件不存在时应当让 `/<slug>` 自定义页面兜底接手，而不是直接把 404 定死。

    注意它**不是** `HttpError`：抛它不会产生响应，而是继续匹配。
    直接把它往上抛给用户是逻辑错误（dispatch 一定会接住）。
    """


def _static_first(route):
    """排序键：静态段多于动态段的路由优先（固定路径胜过通配兜底）。

    注意**不**在这里给多段通配（`<path:x>`）排末位：那属于"兜底之间"的次序，
    由注册顺序表达更直观、也更容易推理
    （见 `features/registry.py`：静态资源注册在自定义页面之前）。
    """
    dynamic = sum(1 for part in route._parts if part.startswith("<"))
    return (dynamic, len(route._parts))


def _is_param(part: str) -> bool:
    return part.startswith("<") and part.endswith(">")


def _param_name(part: str) -> str:
    """`<name>` -> name；`<path:name>` -> name（多段通配，吞掉剩余全部段）。"""
    inner = part[1:-1]
    return inner.split(":", 1)[1] if ":" in inner else inner


def _is_catch_all(part: str) -> bool:
    return part.startswith("<path:")


#: 一个已注册路由
class Route:
    __slots__ = ("path", "methods", "handler", "auth", "permission", "name",
                 "fallback", "_parts", "_catch_all_at")

    def __init__(self, path, methods, handler, auth=PUBLIC, permission=None, name=None,
                 fallback=False):
        self.path = path
        self.methods = tuple(method.upper() for method in methods)
        self.handler = handler
        self.auth = auth
        self.permission = permission
        self.name = name or getattr(handler, "__name__", "handler")
        self.fallback = fallback
        self._parts = tuple(part for part in path.strip("/").split("/") if part)
        #: 多段通配 `<path:x>` 只允许出现在末尾，且只能出现一次
        self._catch_all_at = None
        for index, part in enumerate(self._parts):
            if _is_catch_all(part):
                if index != len(self._parts) - 1:
                    raise ValueError(
                        f"catch-all <path:...> must be the last segment: {path!r}")
                self._catch_all_at = index

    def match(self, path):
        """按段匹配；返回参数字典，不匹配返回 None。

        - `<name>`：匹配**恰好一段**；
        - `<path:name>`：匹配**一段或更多**剩余段（末尾通配，用于静态资源等
          子路径场景），值是未解码的斜杠拼接串。
        """
        parts = tuple(part for part in path.strip("/").split("/") if part)
        if self._catch_all_at is None:
            if len(parts) != len(self._parts):
                return None
        else:
            # 通配前必须有足够段数，且通配至少吃掉一段
            fixed = self._catch_all_at
            if len(parts) <= fixed:
                return None
        params = {}
        for index, expected in enumerate(self._parts):
            if _is_catch_all(expected):
                params[_param_name(expected)] = "/".join(parts[index:])
                break
            actual = parts[index]
            if _is_param(expected):
                params[_param_name(expected)] = actual
            elif expected != actual:
                return None
        return params


class Router:
    """路由表 + 调度。应用只需 register 与 dispatch。"""

    def __init__(self):
        self.routes = []

    def route(self, path, methods=("GET",), auth=PUBLIC, permission=None, name=None,
              fallback=False):
        """装饰器：把处理函数注册到路由表。

        `fallback=True` 表示"兜底路由"（如自定义页面 `/<slug>`）：
        只有当没有任何**声明路由**匹配该路径时才会被考虑，
        因此它不会遮蔽固定路径的方法语义（`GET /logout` 仍然是 405 而不是被
        兜底当成 slug="logout" 的页面 → 404）。
        """
        def decorator(handler):
            self.routes.append(Route(path, methods, handler, auth=auth,
                                     permission=permission, name=name,
                                     fallback=fallback))
            return handler
        return decorator

    def add(self, path, handler, methods=("GET",), auth=PUBLIC, permission=None,
            fallback=False):
        """编程式注册（内部/测试用）。"""
        route = Route(path, methods, handler, auth=auth, permission=permission,
                      fallback=fallback)
        self.routes.append(route)
        return route

    # ---------- 匹配 ----------
    def _candidates(self, path):
        """返回路径匹配的候选路由。

        声明路由优先于兜底路由：只要有任何非 fallback 路由匹配该路径，
        兜底路由就不参与（因此不会遮蔽固定路径的 405 语义）。
        """
        matched = [route for route in self.routes if route.match(path) is not None]
        declared = [route for route in matched if not route.fallback]
        return declared or matched

    def match(self, method, path):
        """返回 (candidates, allowed_methods)。

        `candidates` 是按优先级排好的 `[(route, params), ...]`：调用方逐个尝试，
        排在前面的没命中（抛 `RouteMiss`）再试下一个。顺序规则：

        1. **声明路由优先于 fallback 路由**——`/<slug>` 不能遮蔽 `/logout` 的
           405 语义；
        2. 同组内**静态段多的优先**——固定路径胜过动态段；
        3. 同组且同样"具体"的，**保持注册顺序**——这样"静态文件 vs 页面兜底"
           这类先后关系由 `features/registry.py` 的注册顺序直接表达。

        `allowed_methods` 非空表示"路径存在但方法不允许" -> 405 + Allow。

        含空段（`//`、多余结尾斜杠）的路径直接视为不存在：否则 `strip("/")`
        会吞掉空段，让 `/article//comment` 意外命中 `/article/comment`。
        """
        if path != "/" and ("//" in path or path.endswith("/")):
            return (), ()
        candidates = self._candidates(path)
        if not candidates:
            return (), ()

        declared = [route for route in candidates if not route.fallback]
        fellback = [route for route in candidates if route.fallback]
        ordered = (sorted(declared, key=_static_first)
                   + sorted(fellback, key=_static_first))

        # HEAD 可以由 GET 路由满足（RFC 7231）
        runnable = [(route, route.match(path)) for route in ordered
                    if method in route.methods
                    or (method == "HEAD" and "GET" in route.methods)]
        if runnable:
            return tuple(runnable), ()

        allowed = sorted({m for route in candidates for m in route.methods})
        if "GET" in allowed:
            allowed.append("HEAD")
        return (), tuple(allowed)

    # ---------- 调度 ----------
    async def dispatch(self, request, *, not_found=None, forbidden=None):
        """执行一个请求：匹配 -> 逐个尝试候选路由 -> 安全闸门 -> handler。

        `not_found` / `forbidden` 是可选的自定义处理函数（Feature 用它们渲染
        带布局的错误页）；未提供时回落到 Core 的纯文本响应。
        """
        candidates, allowed = self.match(request.method, request.path)

        if not candidates and allowed:
            # 路径存在但方法不允许：405 必须带 Allow（否则客户端无法自愈）
            response = error_response(405)
            response.headers.append((b"allow", ", ".join(allowed).encode("ascii")))
            return response

        if not candidates:
            return not_found(request) if not_found else error_response(404)

        for route, params in candidates:
            try:
                return await self._run(route, params, request, forbidden)
            except RouteMiss:
                # 该路由"路径匹配但资源不存在"（例如静态文件缺失）：
                # 继续交给下一个候选，让自定义页面兜底有机会接手。
                continue
        return not_found(request) if not_found else error_response(404)

    async def _run(self, route, params, request, forbidden):
        """跑单个路由：安全闸门 + handler。抛出 `RouteMiss` 表示继续找下一个。"""
        try:
            # --- 默认 CSRF 保护：非安全方法一律校验 ---
            # 状态码用 400（与历史契约一致，也是"表单令牌无效"的常规语义）；
            # 认证/权限失败才是 403。
            if requires_protection(request.method) and not validate_request(request):
                return error_response(400, "Invalid CSRF token")

            # --- 认证声明 ---
            # 路由写 `auth="required"`；`"authenticated"` 是等价同义词
            # （Gate 必须接受两者，否则路由声明会静默失效 = 失效开放）。
            #
            # 友好语义：浏览器导航（GET）跳登录页并带回跳地址；
            # 表单提交/其它方法没有"跳转"可言，直接 403。
            if route.auth in AUTH_REQUIRED_VALUES and not check_permission(
                    request, AUTHENTICATED):
                if request.method == "GET":
                    return _login_redirect(request)
                return forbidden(request) if forbidden else error_response(403)

            # --- 权限声明 ---
            # 已登录但权限不足 -> 403（跳登录页没有意义，重新登录也还是这个身份）；
            # 未登录 -> 同上的友好跳转。
            if route.permission and not check_permission(request, route.permission):
                if request.method == "GET" and not check_permission(
                        request, AUTHENTICATED):
                    return _login_redirect(request)
                return forbidden(request) if forbidden else error_response(403)

            response = route.handler(request, **params)
            if response is None:
                # handler 忘记返回响应：明确报错，而不是静默渲染空白。
                # 用 RuntimeError 让 App 层统一转成 500 并记日志。
                raise RuntimeError(
                    f"handler {route.name} returned None instead of a Response")
            return response
        except RouteMiss:
            raise                      # 交给 dispatch 试下一个候选
        except HttpError as error:
            return error_response(error.status, error.message)
