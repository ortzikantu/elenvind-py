"""SEO 模块：`/robots.txt` 与 `/sitemap.xml`。

站点绝对地址**只**来自 `config.toml` 的 `site_url`——绝不从请求 Host 头推导
（Host 由客户端控制，会造成 Host 头投毒）。未配置时这些文件不输出绝对地址。

数据来源：文章条目由装配层注入的 `articles` 回调提供（见 `elenvind/app.py`），
本模块不 import 任何其它业务模块。
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


def register(router, *, articles):
    """把 SEO 路由装到 router 上。

    `articles`：返回 `[(slug, 最后修改时间或 None), ...]` 的**公开数据源**，
    由装配层注入（`elenvind/app.py` → `blog.sitemap_articles`）。
    显式传参而不是 import 兄弟模块，是"模块之间零 import"这条规则的落点。
    """
    @router.route("/robots.txt", methods=["GET"])
    def robots(request):
        lines = ["User-agent: *", "Allow: /"]
        base = site_base()
        if base:
            lines.append(f"Sitemap: {base}/sitemap.xml")
        return text("\n".join(lines) + "\n", cache_control=_ROBOTS_CACHE)

    @router.route("/sitemap.xml", methods=["GET"])
    def sitemap(request):
        base = site_base()
        entries = []
        if base:
            entries.append(f"        <url><loc>{escape_html(_loc(base, '/'))}</loc></url>")
            for slug, date in articles():
                loc = escape_html(_loc(base, f"/article/{slug}"))
                lastmod = format_date(date)
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
