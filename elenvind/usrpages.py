import logging
from pathlib import Path
from .evmd_parser import evmd_to_html

logger = logging.getLogger(__name__)

# 自定义页面目录位于项目根 /usrpages；文件扩展名 .evmd
USRPAGES_DIR = Path(__file__).resolve().parent.parent / "usrpages"
MAX_PAGE_SIZE = 512 * 1024  # 512 KB

_pages_cache = {}          # slug -> 渲染后的 HTML
_file_stats = {}           # 文件状态快照，用于热更新

def _get_file_stats():
    """获取目录下所有 .evmd 文件的状态字典。"""
    stats = {}
    if USRPAGES_DIR.exists():
        for f in USRPAGES_DIR.glob("*.evmd"):
            try:
                st = f.stat()
                stats[f.name] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    return stats

def _has_changes():
    """检查自定义页面目录是否有文件变化。"""
    global _file_stats
    current_stats = _get_file_stats()
    if current_stats != _file_stats:
        _file_stats = current_stats
        return True
    return False

def _load_pages():
    """重新扫描目录，渲染所有 EVMD 页面并缓存。"""
    global _pages_cache
    _pages_cache = {}

    if not USRPAGES_DIR.exists():
        return

    for md_file in USRPAGES_DIR.glob("*.evmd"):
        try:
            if md_file.stat().st_size > MAX_PAGE_SIZE:
                logger.error(f"Custom page file too large: {md_file.name}")
                continue
            content = md_file.read_text(encoding="utf-8")
            html = evmd_to_html(content)  # 自定义页面为纯正文，不带文档头
            slug = md_file.stem               # 文件名去掉扩展名作为 slug
            _pages_cache[slug] = html
        except Exception as e:
            logger.error(f"Failed to load custom page {md_file.name}: {e}")

def get_page(slug: str):
    """
    获取自定义页面的 HTML。
    若页面不存在或目录有变化，会自动重新扫描。
    """
    if _has_changes() or not _pages_cache:
        _load_pages()
    return _pages_cache.get(slug)

def load_pages():
    """强制重新加载所有自定义页面（例如应用启动时调用）。"""
    global _file_stats
    _file_stats = _get_file_stats()
    _load_pages()
