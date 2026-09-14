from .view_layout_base import render as layout
from .i18n import t
from .utils import escape_html

def render(user=None, theme=None, path=None, lang="en"):
    title = t(lang, "page404_title")
    desc = t(lang, "page404_desc")
    content = f"""
    <h1>{escape_html(title)}</h1>
    <p>{escape_html(desc)}</p>"""
    return layout(title, content, user=user, theme=theme, path=path, lang=lang)
