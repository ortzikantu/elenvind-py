"""SEO 元文件响应：/robots.txt 与 /sitemap.xml（纯文本/XML，零依赖）。

站点基址用“请求自身的 Host 头 + 是否 HTTPS”推导，跟随实际部署域名自动变化，
无需在配置里重复维护站点 URL；Host 缺失时 robots 不输出 Sitemap 行。
robots 缓存 1 小时、sitemap 缓存 10 分钟（文章增删后最迟 10 分钟被搜索引擎看到）。
"""
from urllib.parse import quote

from .articles import get_articles
from .http_base import plain_response
from .utils import escape_html, format_date

# 自定义 Cache-Control（http.py 的 _send 检测到 cache-control 时不再叠加默认 no-store）
_ROBOTS_CACHE = (b"cache-control", b"public, max-age=3600")
_SITEMAP_CACHE = (b"cache-control", b"public, max-age=600")


def _site_base(ctx) -> str:
    """根据请求头推导站点绝对基址（如 https://example.com），无 Host 返回空串。"""
    host = ""
    for name, value in ctx.scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1").strip()
            break
    if not host:
        return ""
    scheme = "https" if ctx.secure else "http"
    return f"{scheme}://{host}"


def _loc_url(base: str, path: str) -> str:
    """拼接绝对 URL：对 path 做百分号编码（保留 / 与 RFC 3986 非保留字符）。"""
    return base + quote(path, safe="/")


def seo_robots_get(ctx):
    """GET /robots.txt：全站放行 + 指向 sitemap。"""
    lines = ["User-agent: *", "Allow: /"]
    base = _site_base(ctx)
    if base:
        lines.append(f"Sitemap: {base}/sitemap.xml")
    return plain_response("\n".join(lines) + "\n",
                          content_type="text/plain; charset=utf-8",
                          headers=[_ROBOTS_CACHE])


def seo_sitemap_get(ctx):
    """GET /sitemap.xml：首页 + 全部文章页（含 lastmod，若文章元数据里有）。"""
    base = _site_base(ctx)
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
