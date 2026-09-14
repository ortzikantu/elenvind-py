from .view_partials_head import render as head
from .view_partials_navbar import render as navbar
from .view_partials_footer import render as footer

from .view_partials_head import render as head
from .view_partials_navbar import render as navbar
from .view_partials_footer import render as footer
from .utils import escape_html

def render(title: str, content: str, user=None, theme=None, path=None, lang="en") -> str:
    # 页面语言：http 层解析结果（Cookie/Accept-Language/配置兜底），用于 <html lang>
    safe_lang = escape_html(lang)
    theme_attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""

    return f"""<!DOCTYPE html>
<html lang="{safe_lang}"{theme_attr}>
{head(title)}
<body>
{navbar(user=user, theme=theme, path=path, lang=lang)}
<main>
{content}
</main>
{footer(lang=lang)}
</body>
</html>"""
