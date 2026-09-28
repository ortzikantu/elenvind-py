from .version import get_version
from .config import config
from .db_user import get_user_number
from .i18n import t
from .utils import escape_html
from datetime import datetime

def _nav_link_html(item) -> str:
    """把一条 nav 配置转成 <li>；条目缺 name 或 url 时返回空串。"""
    if not isinstance(item, dict):
        return ""
    name = str(item.get("name") or "").strip()
    url = str(item.get("url") or "").strip()
    if not name or not url:
        return ""
    return f'<li><a href="{escape_html(url)}">{escape_html(name)}</a></li>'

def render(lang: str = "en") -> str:
    params = config.get("params") or {}
    links = [_nav_link_html(item) for item in (params.get("nav") or [])]
    nav_list = "\n".join(f"{link}" for link in links if link)

    # 文案用 raw 值格式化，格式化完成后再整体转义一次：
    # 否则配置里的 & < 会被转义两次，页面上出现 &amp; 字面量
    copyright_name = str(config.get("copyright", "title"))
    year = datetime.now().year
    users_line = escape_html(t(lang, "footer_users", total=get_user_number()))
    rights_line = escape_html(t(lang, "footer_rights", year=year, name=copyright_name))
    powered_line = escape_html(t(lang, "footer_powered", version=get_version()))

    return f"""
    <footer>
        <div class="footer-top">
            <ul>
                {nav_list}
                <li>{users_line}</li>
            </ul>
        </div>
        <div class="footer-bottom">
            <span>{rights_line}</span>
            <span>{powered_line}</span>
        </div>
    </footer>"""
