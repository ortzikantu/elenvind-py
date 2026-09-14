"""文章页渲染（文章正文 + 评论区）。

评论列表与表单的渲染位于 view_partials_comment.py；
本视图仅负责组装文章正文，并将评论区委托给该模块渲染。
"""
import logging

from .config import config
from .view_layout_base import render as layout
from .i18n import t
from .utils import escape_html, format_date
from .articles import get_article_by_slug, load_article_content
from .view_partials_comment import render_comments

logger = logging.getLogger(__name__)


def render(slug: str = None, user=None, reply_to: int = None, csrf_token: str = None,
           theme=None, path=None, lang="en"):
    """渲染文章页。

    - slug        ：文章文件名（不含扩展名）
    - user        ：当前用户记录（匿名时为 None）
    - reply_to    ：回复表单要预填的评论 ID（?reply_to=N）
    - csrf_token  ：与 CSRF Cookie 配对的 Double-Submit CSRF 值
    - theme/path：请求级偏好（透传给布局，用于 data-theme 与切换链接）；
      lang 为站点级界面语言（config.toml locale），同样透传布局
    """
    base_title = config.get("title", "WHERE IS YOUR TITLE?")

    if not slug:
        content = f"<p>{escape_html(t(lang, 'article_none'))}</p>"
        return layout(base_title, content, user=user, theme=theme, path=path, lang=lang)

    article_meta = get_article_by_slug(slug)
    if not article_meta:
        content = f"<p>{escape_html(t(lang, 'article_not_found'))}</p>"
        return layout("404 Not Found", content, user=user, theme=theme, path=path, lang=lang)

    try:
        # load_article_content 已由 EVMD 引擎完成文档头解析与正文渲染
        metadata, body_html = load_article_content(slug)
        article_title = escape_html(metadata.get("title", article_meta["title"]))
        date_pub = format_date(metadata.get("date"))
        date_mod = format_date(metadata.get("lastmod"))
        authors = ", ".join(escape_html(a) for a in metadata.get("authors", []))
    except Exception as e:
        logger.error(f"Article rendering failed for {slug}: {e}")
        content = f"<p>{escape_html(t(lang, 'article_failed'))}</p>"
        return layout("500 Internal Server Error", content, user=user, theme=theme, path=path, lang=lang)

    published_label = escape_html(t(lang, "article_published"))
    updated_label = escape_html(t(lang, "article_updated"))
    unknown_text = escape_html(t(lang, "article_unknown"))
    author_label = escape_html(t(lang, "article_author"))
    pub_html = f'<p class="article-meta">{published_label}: <span>{escape_html(date_pub) if date_pub else unknown_text}</span></p>'
    mod_html = ""
    if date_mod:
        mod_html = f'<p class="article-meta">{updated_label}: <span>{escape_html(date_mod)}</span></p>'

    # 注：<main> 由 view_layout_base 提供，此处不再输出
    content = f"""
        <article>
            <h2>{article_title}</h2>
            {pub_html}
            {mod_html}
            <p><strong>{author_label}:</strong> {authors}</p>
            <hr>
            {body_html}
        </article>
        <p><a href="/">{escape_html(t(lang, 'article_back_home'))}</a></p>"""

    content += render_comments(slug, user, reply_to, csrf_token, lang=lang)
    return layout(article_title, content, user=user, theme=theme, path=path, lang=lang)
