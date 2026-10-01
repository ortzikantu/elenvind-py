"""请求级上下文：让模板全局（`csrf_input()` / `t()`）拿到"当前请求"。

为什么需要它：
- `{{ csrf_input() }}` 要求模板作者不需要手动传令牌；令牌是**每请求**的，
  所以必须有一个请求级的作用域来承载它。
- `{{ t("key") }}` 同理：界面语言是站点级配置，但模板里逐处传 lang 很啰嗦。

实现选择：`contextvars.ContextVar` 是标准库、对 asyncio 安全（每个任务独立），
比 threading.local 更适合 ASGI。不引入"全局可变状态"——写入点只有
`bind_request()` 一处，且必须在请求结束时用 `reset` 还原。

没有绑定请求时（启动期渲染、离线渲染、单元测试），
`current_request()` 返回 None，相关全局降级为空串/默认语言，不抛异常。
"""
from contextvars import ContextVar

_current = ContextVar("elenvind_current_request", default=None)


def bind_request(request):
    """把请求绑定到当前上下文，返回 token（供 reset）。"""
    return _current.set(request)


def reset_request(token) -> None:
    """还原绑定（必须在 finally 里调用，避免上下文泄漏到下一个请求）。"""
    _current.reset(token)


def current_request():
    """当前请求对象，未绑定时返回 None。"""
    return _current.get()


def current_lang() -> str:
    """当前界面语言；无请求时回退配置 locale（再回退 en）。"""
    request = current_request()
    if request is not None:
        return getattr(request, "lang", "en")
    from .config import config
    from .i18n import normalize
    return normalize(config.get("locale", "en"))


def build_render_context(context: dict) -> dict:
    """给模板上下文补齐 Core 提供的公共变量。

    Feature 只提供业务数据；以下变量由 Core 补齐，Feature 不需要关心：
    request / user / csrf_token / lang / site_title / description / keywords /
    theme / admin_badge / copyright_name / current_year / user_count /
    nav_items / social_items / project_items。

    这些正是 `base.html` 与 partials 需要的"框架级"变量。
    显式传入的同名键优先（Feature 可覆盖）。
    """
    from datetime import datetime

    from .config import config

    request = current_request()
    merged = dict(context)
    merged.setdefault("request", request)
    # 用户对象是 sqlite3.Row（不支持 getattr），必须用下标访问
    if request is not None:
        try:
            merged.setdefault("user", request.user)
        except AttributeError:
            merged.setdefault("user", None)
    else:
        merged.setdefault("user", None)
    merged.setdefault("csrf_token", request.csrf_token() if request else "")
    merged.setdefault("lang", current_lang())
    merged.setdefault("config", config)
    merged.setdefault("site_title", config.get("title", "Elenvind"))

    # 主题偏好（Cookie -> data-theme / 切换目标）
    theme = request.cookies.get("theme") if request else None
    merged.setdefault("theme", theme if theme in ("light", "dark") else None)

    # SEO meta
    merged.setdefault("description", config.get("description") or "")
    keywords = config.get("keywords") or ""
    if isinstance(keywords, list):
        keywords = ", ".join(str(item).strip() for item in keywords if str(item).strip())
    merged.setdefault("keywords", str(keywords).strip())

    # 页脚 / 徽章
    merged.setdefault("admin_badge", str(config.get("admin_badge", "BIG BOSS")).strip())
    merged.setdefault("copyright_name", str(config.get("copyright", "title")))
    merged.setdefault("current_year", datetime.now().year)
    if "user_count" not in merged:
        from .db_user import get_user_number
        merged["user_count"] = get_user_number()

    params = config.get("params") or {}
    merged.setdefault("nav_items", _nav_items(params))
    merged.setdefault("social_items", _social_items(params))
    merged.setdefault("project_items", _project_items(params))
    return merged


def _nav_items(params):
    """导航条目：只保留 name/url 都齐全的配置项。"""
    items = []
    for entry in params.get("nav") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if name and url:
            items.append({"name": name, "url": url})
    return items


def _social_items(params):
    """社交条目：必须给出 url；icon/name 可选。"""
    items = []
    for entry in params.get("social") or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        items.append({
            "name": str(entry.get("name") or "").strip(),
            "url": url,
            "icon": str(entry.get("icon") or "").strip(),
        })
    return items


def _project_items(params):
    """项目条目：name/url 至少有一个。"""
    items = []
    for entry in params.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name and not url:
            continue
        items.append({
            "name": name,
            "url": url,
            "description": str(entry.get("description") or entry.get("desc") or "").strip(),
        })
    return items
