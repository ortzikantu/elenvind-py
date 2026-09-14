from .config import config
from .view_layout_base import render as layout
from .utils import escape_html

def render(page_html: str, slug: str, user=None, theme=None, path=None, lang="en"):
    title = config.get("title", "WHERE IS YOUR TITLE?")
    safe_slug = escape_html(slug)  # 防止 XSS
    content = f"""
    <article>
        {page_html}
    </article>
    """
    return layout(f"{safe_slug} - {title}", content, user=user, theme=theme, path=path, lang=lang)
