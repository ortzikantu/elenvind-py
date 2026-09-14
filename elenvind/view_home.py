"""首页视图：仿 hexo-theme-cactus 的首页布局。

区块从上到下依次为（均为可选，未配置即自动省略）：
1. about    —— intro 介绍段落与 "Find me on" 社交链接行；
2. writing  —— 文章列表（日期 + 标题，超过一页时自动分页）；
3. projects —— config.toml 中 [[params.projects]] 条目列表。

界面文案（区块标题 / 社交句框 / 分页 / 空态）统一走 i18n（语言由请求层解析）。
"""
from .config import config
from .articles import get_articles
from .i18n import t
from .utils import escape_html, format_date
from .view_layout_base import render as layout


def _external_attrs(url: str) -> str:
    """站外链接加 新窗口 + noopener；站内路径（/、# 开头）不加。"""
    if url.startswith(("http://", "https://", "//")):
        return ' target="_blank" rel="noopener"'
    return ""

def _hero_html() -> str:
    """hero 区：全宽背景图，位于 about 之上。配置项为 params.hero（图片 URL）。"""
    static_cfg = config.get("static", {})
    hero = static_cfg.get("hero") or ""
    if not hero:
        return ""
    # 对 URL 进行 HTML 转义，防止特殊字符破坏属性
    safe_hero = escape_html(hero)
    return f'<section class="home-hero"><div class="hero" style="background-image:url({safe_hero})" loading="lazy"></div></section>'

def _intro_html(params) -> str:
    """about 区介绍段落：params.intro 存在时才输出。"""
    text = str(params.get("intro") or "").strip()
    if not text:
        return ""
    return f'<p class="intro">{escape_html(text)}</p>'

def _social_html(params, lang: str) -> str:
    """about 区社交行："Find me on …"。social 未配置时返回空串。"""
    entries = [
        item for item in (params.get("social") or [])
        if isinstance(item, dict) and (item.get("url") or "").strip()
    ]
    total = len(entries)
    if total == 0:
        return ""
    prefix = t(lang, "social_prefix")
    separator = t(lang, "social_sep")
    joiner = t(lang, "social_join")
    period = t(lang, "social_end")

    anchors = []
    for item in entries:
        name = str(item.get("name") or "").strip()
        safe_name = escape_html(name)
        url = str(item.get("url") or "#").strip()
        safe_url = escape_html(url)
        icon = str(item.get("icon") or "").strip()
        if icon:
            # 有图标时用图片展示（alt 提供无障碍名称与加载失败占位文本）
            inner = f'<img src="{escape_html(icon)}" alt="{safe_name or "link"}" loading="lazy">'
        else:
            # 无图标时退化为纯文字链接
            inner = safe_name or "link"
        if name:
            label = f' aria-label="{safe_name}" title="{safe_name}"'
        else:
            label = ' aria-hidden="true"'
        anchors.append(
            f'<a class="social-icon" href="{safe_url}"{_external_attrs(url)}{label}>{inner}</a>'
        )

    # 多个平台时：中间用分隔符，倒数第二个与最后一个之间用连接词
    pieces = []
    for index, anchor in enumerate(anchors):
        pieces.append(anchor)
        if index < total - 2:
            pieces.append(separator)
        elif index == total - 2:
            pieces.append(joiner)
    return f'<p class="socials">{prefix}{"".join(pieces)}{period}</p>'

def _about_html(params, lang: str) -> str:
    """组合 about 区；介绍段落与社交行都为空时整区省略。"""
    intro = _intro_html(params)
    social = _social_html(params, lang)
    body = "\n        ".join(part for part in (intro, social) if part)
    if not body:
        return ""
    return f'<section class="home-about">\n        {body}\n        </section>'


def _writing_html(page: int, lang: str) -> str:
    """writing 区块：cactus 式文章列表（日期灰 + 加粗标题），支持分页。"""
    per_page = int(config.get("pagination", {}).get("per_page", 10) or 10)
    articles = get_articles()
    total_articles = len(articles)
    total_pages = (total_articles + per_page - 1) // per_page if total_articles else 0

    # 页码越界时收拢到合法范围
    page = max(1, min(page, total_pages) if total_pages else 1)
    page_articles = articles[(page - 1) * per_page:page * per_page]

    if page_articles:
        items = []
        for article in page_articles:
            article_title = escape_html(article["title"])
            article_date = format_date(article.get("date"))
            slug = escape_html(article["slug"])
            date_attr = escape_html(article.get("date"))
            items.append(
                f'<li><div class="article-date"><time datetime="{date_attr}">'
                f"{escape_html(article_date)}</time></div> "
                f'<span><a href="/article/{slug}">{article_title}</a></span></li>'
            )
        list_html = "<ul>" + "".join(items) + "</ul>"
    else:
        list_html = f"<p>{escape_html(t(lang, 'home_no_articles'))}</p>"

    # 分页导航（仅总页数 > 1 时输出）
    pagination_html = ""
    if total_pages > 1:
        pagination_html = '<nav class="pagination" aria-label="Pagination">'
        if page > 1:
            pagination_html += f'<a href="?page={page - 1}"><strong><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="18,4 18,20 4,12"></polygon></svg></strong></a>'
        pagination_html += escape_html(t(lang, "home_page_of", page=page, total=total_pages))
        if page < total_pages:
            pagination_html += f'<a href="?page={page + 1}"><strong><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="6,4 6,20 20,12"></polygon></svg></strong></a>'
        pagination_html += "</nav>"

    heading = escape_html(t(lang, "home_writing"))
    return f"""<section class="article-list">
        <h2>{heading} ({total_articles})</h2>
        {list_html}
        {pagination_html}
        </section>"""


def _projects_html(params, lang: str) -> str:
    """projects 区块：加粗名称链接 + 冒号 + 描述；未配置条目时整区省略。"""
    entries = [
        item for item in (params.get("projects") or [])
        if isinstance(item, dict)
        and ((item.get("name") or "").strip() or (item.get("url") or "").strip())
    ]
    if not entries:
        return ""

    items = []
    for item in entries:
        name = str(item.get("name") or "").strip()
        safe_name = escape_html(name)
        url = str(item.get("url") or "").strip()
        safe_url = escape_html(url)
        description = str(item.get("description") or item.get("desc") or "").strip()

        if name and url:
            name_html = f'<a href="{safe_url}"{_external_attrs(url)}>{safe_name}</a>'
        elif name:
            # 只有名称没有链接：降级为不可点击文本
            name_html = f'<span class="project-name">{safe_name}</span>'
        else:
            # 只有链接没有名称：直接用链接地址作文字
            name_html = f'<a href="{safe_url}"{_external_attrs(url)}>{safe_url}</a>'

        if description:
            items.append(f"<li>{name_html} <span>{escape_html(description)}</span></li>")
        else:
            items.append(f"<li>{name_html}</li>")

    heading = escape_html(t(lang, "home_projects"))
    list_items = "\n".join(f"        {item}" for item in items)
    return f"""<section class="project-list">
        <h2>{heading}</h2>
        <ul>
        {list_items}
        </ul>
        </section>"""


def render(user=None, page=1, theme=None, path=None, lang="en"):
    title = config.get("title", "WHERE IS YOUR TITLE?")
    params = config.get("params") or {}

    sections = [
        section for section in (
            _hero_html(),
            _about_html(params, lang),
            _writing_html(page, lang),
            _projects_html(params, lang),
        )
        if section
    ]
    content = "\n\n        ".join(sections)
    return layout(title, content, user=user, theme=theme, path=path, lang=lang)
