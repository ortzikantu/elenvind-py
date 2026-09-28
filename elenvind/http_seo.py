"""SEO 元文件响应：/robots.txt 与 /sitemap.xml（纯文本/XML，零依赖）。

站点基址只来自 `config.toml` 顶层的 `site_url`：
绝不使用请求的 Host 头拼绝对 URL——Host 由客户端完全控制，
用它生成 canonical / sitemap 会把任意域名写进给搜索引擎的元文件（Host 头投毒）。
`site_url` 留空时 robots 不输出 Sitemap 行、sitemap 输出空 urlset（不产生错误 URL）。
robots 缓存 1 小时、sitemap 缓存 10 分钟（文章增删后最迟 10 分钟被搜索引擎看到）。
"""
from urllib.parse import quote

from .articles import get_articles
from .config import config
from .http_base import plain_response
from .utils import escape_html, format_date

# 自定义 Cache-Control（http.py 的 _send 检测到 cache-control 时不再叠加默认 no-store）
_ROBOTS_CACHE = (b"cache-control", b"public, max-age=3600")
_SITEMAP_CACHE = (b"cache-control", b"public, max-age=600")


def site_base() -> str:
    """站点绝对基址（如 https://example.com），未配置时返回空串。"""
    return str(config.get("site_url", "") or "").strip().rstrip("/")


def _loc_url(base: str, path: str) -> str:
    """拼接绝对 URL：对 path 做百分号编码（保留 / 与 RFC 3986 非保留字符）。"""
    return base + quote(path, safe="/")


def seo_robots_get(ctx):
    """GET /robots.txt：全站放行 + 指向 sitemap（有 site_url 时才给 Sitemap 行）。"""
    lines = ["User-agent: *", "Allow: /"]
    base = site_base()
    if base:
        lines.append(f"Sitemap: {base}/sitemap.xml")
    return plain_response("\n".join(lines) + "\n",
                          content_type="text/plain; charset=utf-8",
                          headers=[_ROBOTS_CACHE])


def seo_sitemap_get(ctx):
    """GET /sitemap.xml：首页 + 全部文章页（含 lastmod，若文章元数据里有）。"""
    base = site_base()
    entries = []
    if base:
        entries.append(f"        <url><loc>{escape_html(_loc_url(base, '/'))}</loc></url>")
        for article in get_articles():
            loc = escape_html(_loc_url(base, f"/article/{article['slug']}"))
            lastmod = format_date(article.get("lastmod") or article.get("date"))
            if lastmod:
                entries.append(
                    f"        <url><loc>{loc}</loc><lastmod>{escape_html(lastmod)}</lastmod></url>"
                )
            else:
                entries.append(f"        <url><loc>{loc}</loc></url>")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return plain_response(body, content_type="application/xml; charset=utf-8",
                          headers=[_SITEMAP_CACHE])
