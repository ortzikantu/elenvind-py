"""请求级上下文：让模板全局（`csrf_input()` / `t()`）拿到"当前请求"。

为什么需要它：
- `{{ csrf_input() }}` 要求模板作者不需要手动传令牌；令牌是**每请求**的，
  所以必须有一个请求级的作用域来承载它。
- `{{ t("key") }}` 同理：界面语言是站点级配置，但模板里逐处传 lang 很啰嗦。

实现选择：`contextvars.ContextVar` 是标准库，每个线程 / 协程上下文各自独立，
因此对"同步 WSGI worker（每请求一线程）"与 gthread 线程池都成立。
不引入"全局可变状态"——写入点只有 `bind_request()` 一处，
且必须在请求结束时用 `reset` 还原。

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


#: 用户总数提供者（装配层注入，例如 `db.get_user_number`）。
#: core 不认识 db，所以这里只保留一个显式的注入点，而不是去 import 持久化层。
_user_count_provider = None


def set_user_count_provider(provider) -> None:
    """装配层注入"用户总数"数据源（`elenvind/app.py` 调用）。"""
    global _user_count_provider
    _user_count_provider = provider


def build_render_context(context: dict) -> dict:
    """给模板上下文补齐 Core 提供的公共变量。

    模块只提供业务数据；以下变量由 Core 补齐，模块不需要关心：
    request / user / csrf_token / lang / site_title / description / keywords /
    theme / admin_badge / copyright_name / current_year / user_count /
    nav_items / social_items / project_items。

    这些正是 `base.html` 与 partials 需要的"框架级"变量。
    显式传入的同名键优先（模块可覆盖）。
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
    site_title = config.get("title") or "Elenvind"
    merged.setdefault("site_title", site_title)

    # 主题偏好（Cookie -> data-theme / 切换目标）。
    # 走 request.preference() 而不是直接读 cookies：偏好命名与允许值由
    # core.security.PREFERENCE_COOKIES 统一定义，白名单校验也在那里。
    merged.setdefault("theme", request.preference("theme") if request else None)

    # SEO meta
    merged.setdefault("description", config.get("description") or "")
    keywords = config.get("keywords") or ""
    if isinstance(keywords, list):
        keywords = ", ".join(str(item).strip() for item in keywords if str(item).strip())
    merged.setdefault("keywords", str(keywords).strip())

    # 页脚 / 徽章
    merged.setdefault("admin_badge", str(config.get("admin_badge", "BIG BOSS")).strip())
    # 版权署名缺省回落到站点名（再回落到 "Elenvind"）。
    #
    # 旧代码是 `config.get("copyright", "title")` —— 两处都错：
    #   1. 那个 "title" 是**字面量字符串**，不是"取 title 键"的意思。
    #      键缺失时页脚会显示 "© 2026 title"；键存在但为 None（TOML 里
    #      `copyright = ""` 或整行注释掉后 load_config 补 None）时，
    #      `.get` 返回那个 None，页脚显示 "© 2026 None"。
    #   2. 从未有人注意到，因为项目自带的 config.toml 恰好设了 copyright。
    copyright_name = config.get("copyright")
    if not copyright_name or not str(copyright_name).strip():
        copyright_name = site_title
    merged.setdefault("copyright_name", str(copyright_name))
    merged.setdefault("current_year", datetime.now().year)
    if "user_count" not in merged:
        # 用户总数来自数据库，但 core 不认识 db：由装配层注入 provider
        # （见 `set_user_count_provider`；未注入时按 0 处理，模板照常渲染）。
        merged["user_count"] = _user_count_provider() if _user_count_provider else 0

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
