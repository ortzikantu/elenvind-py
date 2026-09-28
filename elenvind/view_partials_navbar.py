"""顶栏导航局部模板：仿 hexo-theme-cactus 头部布局。

结构（与 cactus 的 header.ejs 一致）：
- 站标图标 + 站名包在同一个 <a class="header-brand"> 里，整体居左；
- 导航条目居右，条目来自 config.toml 的 [[params.nav]]（每条 name + url）；
- 末尾固定追加：语言选择（details 下拉，零 JS）、主题切换、用户入口；
- 纯 CSS 布局、零 JS，窄屏自动换行。
"""
from .config import config
from .i18n import t
from .utils import escape_html

def _user_link_html(user, admin_badge: str, lang: str) -> str:
    """渲染导航末尾的用户入口；未登录时显示 Sign In（按语言取词）。

    admin_badge 传空串（配置里显式留空）时即使管理员也不渲染徽章。"""
    if not user:
        return f'<a href="/login">{escape_html(t(lang, "nav_sign_in"))}</a>'
    nickname = f'{escape_html(user["nickname"])} (#{user["id"]})'
    badge = ""
    if admin_badge and user["id"] == 1:
        badge = f' <span class="badge-admin">{escape_html(admin_badge)}</span>'
    return f'<a href="/user">{nickname}</a>{badge}'


def _theme_link_html(theme, path, lang: str) -> str:
    """渲染主题切换项：目标是当前主题的反色（未设置时按亮色处理，链接切到深色）。

    next 传回当前站内路径（由 http 层保证以单个 / 开头），切换后原地跳转。
    """
    target = "light" if theme == "dark" else "dark"
    darkicon = f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24"><path fill="var(--text)" d="M10 2c-1.82 0-3.53.5-5 1.35C8 5.08 10 8.3 10 12s-2 6.92-5 8.65C6.47 21.5 8.18 22 10 22a10 10 0 0 0 10-10A10 10 0 0 0 10 2" /></svg>'
    lighticon = f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24"><path fill="var(--text)" d="M12 18a6 6 0 0 1-6-6a6 6 0 0 1 6-6a6 6 0 0 1 6 6a6 6 0 0 1-6 6m8-2.69L23.31 12L20 8.69V4h-4.69L12 .69L8.69 4H4v4.69L.69 12L4 15.31V20h4.69L12 23.31L15.31 20H20z"/></svg>'
    label = lighticon if theme == "dark" else darkicon
    safe_path = escape_html(path) if path else "/"
    title = escape_html(t(lang, "nav_theme_title"))
    return (
        f'<li class="nav-theme"><a href="/theme?mode={target}&next={safe_path}" '
        f'title="{title}">{label}</a></li>'
    )


def _brand_html(title: str) -> str:
    """站标 + 站名组成一个整体链接。

    图标 URL 取 config.toml [static].logo，留空回退 [static].favicon；
    两级都留空则只显示站名（不输出 <img>，避免裂图）。
    取值来自管理员配置，但进入 HTML 属性仍统一转义。
    """
    static_cfg = config.get("static") or {}
    logo_url = str(static_cfg.get("logo") or static_cfg.get("favicon") or "").strip()
    icon_html = f'<img src="{escape_html(logo_url)}" alt="" loading="lazy">' if logo_url else ""
    return (
        f'<a class="header-brand" href="/">'
        f'{icon_html}<span class="header-title">{escape_html(title)}</span>'
        f"</a>"
    )


def render(user=None, theme=None, path=None, lang="en") -> str:
    title = config.get("title", "WHERE IS YOUR TITLE?")
    admin_badge = str(config.get("admin_badge", "BIG BOSS")).strip()

    links = []
    links.append(f'<li class="nav-user">{_user_link_html(user, admin_badge, lang)}</li>')
    links.append(_theme_link_html(theme, path, lang))
    nav_list = "\n".join(f"            {link}" for link in links if link)

    return f"""
    <header>
        {_brand_html(title)}

        <nav class="header-nav">
            <ul>
            {nav_list}
            </ul>
        </nav>
    </header>
    """
