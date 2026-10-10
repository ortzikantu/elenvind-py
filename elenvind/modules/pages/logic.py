"""Pages 模块：自定义页面（custom_pages/*.md）。

页面是**纯 Markdown 正文**（无文档头），文件名即路由：`about.md` -> `/about`。
路径安全：slug 先过白名单校验；页面内容是扫描目录得到的，URL 只做字典查表，
从不参与文件路径拼接。

依赖：只依赖 `core/`（含共享的内容格式原语 `core.content`）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ...core.config import ROOT, config, resolve_path
from ...core.content import ContentError, validate_slug
from ...core.markdown import render_markdown

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 512 * 1024

_pages_cache = None       # slug -> Markup
_file_stats = None
#: 目录状态检查的最小间隔（秒）：命中缓存的 `/<slug>` 兜底请求不该每次都
#: glob 整个 custom_pages 目录（与 blog 模块同一策略）。
_SCAN_INTERVAL_SECONDS = 2.0
_last_scan_at = 0.0


def custom_pages_dir() -> Path:
    """自定义页面目录：config.toml 顶层 `custom_pages_dir`（
    键名沿用历史命名以保持配置兼容），默认 `custom_pages/`。"""
    return resolve_path(config.get("custom_pages_dir"), ROOT / "custom_pages")


def _get_file_stats(dir_path: Path):
    stats = {}
    if dir_path.exists():
        for path in dir_path.glob("*.md"):
            try:
                stat = path.stat()
                stats[path.name] = (stat.st_mtime, stat.st_size)
            except OSError:
                continue
    return stats


def _load_pages(dir_path: Path, stats: dict):
    """扫描目录渲染全部页面；单文件失败不影响其它页面，只记日志。

    只返回成功的页面。曾经这里还收集一个 `failed` 字典
    （{文件名: 上次快照}）并一路返回，但**没有任何调用方读它** ——
    调用方写的是 `new_pages, _failed = ...`，下划线就说明了一切。
    失败信息已经由 logger.error 记录，那个字典纯粹是死数据。
    """
    pages = {}
    if not dir_path.exists():
        return pages
    for path in dir_path.glob("*.md"):
        try:
            if path.stat().st_size > MAX_PAGE_SIZE:
                raise ContentError(f"page too large: {path.name}")
            pages[path.stem] = render_markdown(path.read_text(encoding="utf-8"))
        except (ContentError, OSError, UnicodeDecodeError, ValueError) as exc:
            logger.error("Failed to load page %s: %s", path.name, exc)
    return pages


def _rescan() -> None:
    """一次性提交快照与页面缓存（失败时两者都保持不变）。"""
    global _pages_cache, _file_stats, _last_scan_at
    directory = custom_pages_dir()
    new_stats = _get_file_stats(directory)
    new_pages = _load_pages(directory, new_stats)
    _file_stats = new_stats
    _pages_cache = new_pages
    _last_scan_at = time.monotonic()


def _has_changes() -> bool:
    """目录是否需要重扫（按 `_SCAN_INTERVAL_SECONDS` 节流）。"""
    global _last_scan_at
    if _file_stats is None:
        return True
    now = time.monotonic()
    if now - _last_scan_at < _SCAN_INTERVAL_SECONDS:
        return False
    _last_scan_at = now
    return _get_file_stats(custom_pages_dir()) != _file_stats


def get_page(slug: str):
    """返回页面 Markup；slug 非法或页面不存在返回 None。"""
    if not validate_slug(slug):
        return None
    if _pages_cache is None or _has_changes():
        _rescan()
    return _pages_cache.get(slug)


def load_pages():
    """启动钩子：强制重扫。"""
    _rescan()
