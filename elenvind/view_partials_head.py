from .utils import escape_html
from .version import get_version
from .config import config

def render(title: str) -> str:
    # 静态资源路径从 config.toml [static] 读取；留空时回退到站点相对路径
    static_cfg = config.get("static", {})
    css_url = static_cfg.get("css") or "/style.css"
    favicon_url = static_cfg.get("favicon") or "/favicon.ico"
    # SEO 描述/关键词：config.toml 顶层有值才输出（description/keywords）。
    # keywords 兼容两种写法：纯字符串（逗号分隔）或数组 ["a", "b"]（自动以逗号连接）
    description = config.get("description") or ""
    keywords_raw = config.get("keywords") or ""
    if isinstance(keywords_raw, list):
        keywords = ", ".join(str(item).strip() for item in keywords_raw if str(item).strip())
    else:
        keywords = str(keywords_raw).strip()
    meta_extra = ""
    if description:
        meta_extra += f'\n        <meta name="description" content="{escape_html(description)}">'
    if keywords:
        meta_extra += f'\n        <meta name="keywords" content="{escape_html(keywords)}">'
    return f"""
    <head>
        <meta charset="utf-8">
        <title>{escape_html(title)}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="generator" content="Elenvind {get_version()}">{meta_extra}
        <link rel="stylesheet" href="{escape_html(css_url)}">
        <link rel="icon" type="image/x-icon" href="{escape_html(favicon_url)}">
    </head>"""
