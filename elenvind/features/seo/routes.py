"""SEO Feature：/robots.txt 与 /sitemap.xml。

站点绝对地址**只**来自 `config.toml` 的 `site_url`——绝不从请求 Host 头推导
（Host 由客户端控制，会造成 Host 头投毒）。未配置时这些文件不输出绝对地址。
"""
from __future__ import annotations

from urllib.parse import quote

from ...core.config import config
from ...core.http import text
from ...core.utils import escape_html, format_date

_ROBOTS_CACHE = "public, max-age=3600"
_SITEMAP_CACHE = "public, max-age=600"


def site_base() -> str:
    """站点绝对基址（如 https://example.com），未配置时返回空串。"""
    return str(config.get("site_url", "") or "").strip().rstrip("/")


def _loc(base: str, path: str) -> str:
    return base + quote(path, safe="/")


def register(router):
    @router.route("/robots.txt", methods=["GET"])
    def robots(request):
        lines = ["User-agent: *", "Allow: /"]
        base = site_base()
        if base:
            lines.append(f"Sitemap: {base}/sitemap.xml")
        return text("\n".join(lines) + "\n", cache_control=_ROBOTS_CACHE)

    @router.route("/sitemap.xml", methods=["GET"])
    def sitemap(request):
        from ..blog import logic as blog

        base = site_base()
        entries = []
        if base:
            entries.append(f"        <url><loc>{escape_html(_loc(base, '/'))}</loc></url>")
            for article in blog.get_articles():
                loc = escape_html(_loc(base, f"/article/{article['slug']}"))
                lastmod = format_date(article.get("lastmod") or article.get("date"))
                if lastmod:
                    entries.append(f"        <url><loc>{loc}</loc>"
                                   f"<lastmod>{escape_html(lastmod)}</lastmod></url>")
                else:
                    entries.append(f"        <url><loc>{loc}</loc></url>")
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + "\n".join(entries) + "\n</urlset>\n")
        return text(body, content_type="application/xml; charset=utf-8",
                    cache_control=_SITEMAP_CACHE)

    return router
