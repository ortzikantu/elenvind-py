"""Core 静态资源服务：把 `elenvind/static/` 目录整体挂在**站点根**发出。

目录约定（与 config.toml 的 `[static]` 对应）：

    elenvind/static/css/    样式表        -> [static].css 为空时用它（缺省 style.css）
    elenvind/static/imgs/   图像          -> [static].favicon 为空时用它（缺省 favicon.*）
    elenvind/static/...     其它任意文件  -> 直接按相对路径取到

URL 与磁盘一一对应，**没有额外前缀**：

    /css/style.css   ->  elenvind/static/css/style.css
    /imgs/logo.png   ->  elenvind/static/imgs/logo.png

因此"往目录里丢一个文件就能被站点用上"，不用维护白名单，也不用记前缀。

与自定义页面的关系：静态文件**优先**。同一路径既有文件又有
`custom_pages/<slug>.md` 时以文件为准（与 Nginx `try_files $uri` 的行为一致）；
没有对应文件时才回落到页面。

安全边界（这是本模块唯一需要小心的地方）：
1. **规范化后必须仍在 STATIC_DIR 之下**——挡 `../`、`..%2f`、绝对路径、
   Windows 盘符与反斜杠等一切越界形态（不能只做字符串前缀比较，
   因为 `static-evil/` 会以前缀骗过它，所以用 `Path.is_relative_to`）。
2. 拒绝隐藏文件/目录（任一段以 `.` 开头），避免 `.git`、编辑器临时文件外泄。
3. 只接受常规文件（不是目录、不是符号链接指向外部）。
4. 文件不存在一律 404，且响应体不透露磁盘路径。

这些资产是**公开**的（浏览器要取），因此不做鉴权。站点私有内容不要放这里。
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path

from .http import Response
from .routing import RouteMiss

logger = logging.getLogger(__name__)

#: 静态根目录（唯一真相源：URL 就是相对本目录的路径）
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: 两个"有缺省语义"的子目录名（模板回退逻辑按名字找它们）
CSS_DIR_NAME = "css"
IMGS_DIR_NAME = "imgs"

#: 路由模式：一段或多段，静态根的相对路径直接作为 URL
ROUTE_PATTERN = "/<path:relative>"

#: 缺省样式表的 URL 路径：`[static].css` 为空且 use_builtin_css 为真时使用
DEFAULT_CSS_PATH = f"/{CSS_DIR_NAME}/style.css"
#: 缺省站点图标的候选（按顺序取第一个存在的）
DEFAULT_ICON_CANDIDATES = (
    f"/{IMGS_DIR_NAME}/favicon.ico",
    f"/{IMGS_DIR_NAME}/favicon.png",
)

#: 浏览器缓存时间（秒）。静态资源改动不频繁，一天够用；
#: ETag 保证"改了文件刷新即刻生效"，不会让浏览器一直用旧副本。
DEFAULT_MAX_AGE = 86400

#: 覆盖标准库的 MIME 表：这些类型必须准确，否则浏览器会拒绝执行/渲染
#: （例如 CSS 若回成 text/plain，严格模式下样式表会被忽略）。
MIME_OVERRIDES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}

#: 这些后缀是文本，按文本发送（便于调试时直接看内容）
_TEXT_SUFFIXES = frozenset({".css", ".js", ".mjs", ".json", ".svg", ".txt", ".md",
                            ".xml", ".webmanifest", ".map"})


def content_type_for(path: Path) -> str:
    """按后缀给出 Content-Type（先查覆盖表，再退回标准库）。"""
    suffix = path.suffix.lower()
    if suffix in MIME_OVERRIDES:
        return MIME_OVERRIDES[suffix]
    guessed, _encoding = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def stylesheet_file() -> Path:
    """缺省样式表的磁盘路径（模板守卫与测试也会用到）。"""
    return STATIC_DIR / CSS_DIR_NAME / "style.css"


def default_icon_path():
    """缺省图标的 URL 路径；候选都不存在时返回 None（此时不应输出 `<link>`）。"""
    for url_path in DEFAULT_ICON_CANDIDATES:
        if (STATIC_DIR / url_path.lstrip("/")).is_file():
            return url_path
    return None


def default_icon_file():
    """缺省图标的磁盘路径；一个都没有时返回 None。"""
    url_path = default_icon_path()
    if url_path is None:
        return None
    return STATIC_DIR / url_path.lstrip("/")


def _has_hidden_segment(parts) -> bool:
    """任一段以 `.` 开头即为隐藏（`.git`、`.env`、编辑器临时文件等）。"""
    return any(part.startswith(".") for part in parts)


def resolve_asset(relative: str):
    """把 URL 里的相对路径解析成 STATIC_DIR 下的真实文件路径。

    返回 `Path`；不合法/不存在/越界/是隐藏文件时返回 None（调用方回 404，
    **不要**把失败原因告诉客户端，避免成为目录探测工具）。
    """
    if not relative:
        return None
    # 统一分隔符后再按段检查：Windows 上 `\\` 也是分隔符
    normalized = relative.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or _has_hidden_segment(parts):
        return None
    candidate = STATIC_DIR.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    root = STATIC_DIR.resolve()
    # 关键：用 is_relative_to 而不是字符串前缀比较。
    # 前缀比较会被 `…/static-evil/x` 骗过，而这里还顺带覆盖了
    # 符号链接指向外部的情况（resolve 之后父链已经展开）。
    if not resolved.is_relative_to(root):
        logger.warning("static asset outside root rejected: %r", relative)
        return None
    # 解析之后再查一次"点开头"的路径段：URL 段检查挡不住
    # `static/x.png -> static/.secret` 这种"根内软链指向根内隐藏文件"。
    if _has_hidden_segment(resolved.relative_to(root).parts):
        logger.warning("static asset resolves into a hidden path: %r", relative)
        return None
    if not resolved.is_file():
        return None
    return resolved


def register(router):
    """把静态资源路由装到 router 上（只读，因此只注册 GET）。

    优先级：固定路由 > 静态文件 > 自定义页面兜底。

    实现上这是一个**多段通配 fallback 路由**：它匹配所有路径，但只用
    `RouteMiss` 表示"磁盘上没有这个文件"，于是调度器会继续尝试下一个候选
    （通常是 `/<slug>` 页面）。因此：

    - `/login` 这类固定路由永远先匹配（声明路由优先于 fallback）；
    - 有文件就用文件；
    - 没有文件才轮到页面兜底。
    """

    @router.route(ROUTE_PATTERN, methods=["GET"], fallback=True)
    def static_asset(request, relative):
        path = resolve_asset(relative)
        if path is None:
            # 没有这个文件 -> 让页面兜底接手（不是 404，因为可能是 /about）
            raise RouteMiss(relative)
        try:
            payload = path.read_bytes()
        except OSError:
            raise RouteMiss(relative) from None

        content_type = content_type_for(path)
        cache_control = f"public, max-age={DEFAULT_MAX_AGE}"
        # 用真实摘要而不是内置 hash()：Python 的 str/bytes hash 每个进程都加盐，
        # 重启后 ETag 会变，浏览器缓存每次重启就失效一次。
        etag = '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'

        # 条件请求：内容没变就回 304（省掉整个响应体）
        if request.header("if-none-match") == etag:
            response = Response(b"", status=304, content_type=content_type,
                                cache_control=cache_control)
            response.headers.append((b"etag", etag.encode("ascii")))
            return response

        if path.suffix.lower() in _TEXT_SUFFIXES:
            try:
                body = payload.decode("utf-8")
            except UnicodeDecodeError:
                body = payload
        else:
            body = payload
        response = Response(body, status=200, content_type=content_type,
                            cache_control=cache_control)
        response.headers.append((b"etag", etag.encode("ascii")))
        return response

    return router
