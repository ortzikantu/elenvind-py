"""自定义页面（usrpages/*.evmd）：slug ↔ 文件名的唯一映射与渲染缓存。

路径安全（核心约定：URL 输入永远不直接决定文件系统路径）：
- 页面 slug 必须先通过 validate_slug()：只允许 [A-Za-z0-9._-]，且不能是 "." / ".."；
- 页面内容是**扫描目录得到**的（glob 出的文件名反过来作为 key），
  请求里的 slug 只做字典查表，从不拼接成路径；
  因此 `../`、绝对路径、NUL、路径分隔符都不可能落到文件系统调用上。

缓存策略与 articles.py 一致：状态快照 + 渲染结果一次性提交，
先扫描出 new_stats/new_pages，全部成功后才替换缓存。
"""
import logging
import re
from pathlib import Path

from .config import config, ROOT, resolve_path
from .evmd_parser import evmd_to_html, EvmdError

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 512 * 1024  # 512 KB

# slug 白名单：只允许字母、数字、点、下划线、连字符（点单独校验，见 validate_slug）
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_pages_cache = None        # slug -> 渲染后的 HTML；None 表示尚未扫描
_file_stats = None         # 文件状态快照，用于热更新


def usrpages_dir() -> Path:
    """自定义页面目录：config.toml 顶层 usrpages_dir（相对项目根），默认 usrpages/。"""
    value = config.get("usrpages_dir", "usrpages")
    return resolve_path(value, ROOT / "usrpages")


def validate_slug(slug: str) -> bool:
    """校验页面 slug；不合法一律拒绝（不进入查表，也不会用于拼路径）。"""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return False
    # 排除 "." / ".." 以及点开头的名字，避免与目录语义混淆
    return slug not in (".", "..") and not slug.startswith(".")


def _get_file_stats(dir_path: Path):
    """获取目录下所有 .evmd 文件的状态字典。"""
    stats = {}
    if dir_path.exists():
        for f in dir_path.glob("*.evmd"):
            try:
                st = f.stat()
                stats[f.name] = (st.st_mtime, st.st_size)
            except OSError:
                continue   # 扫描期间文件被删除/重命名：跳过，下个请求会重扫
    return stats


def _has_changes() -> bool:
    """检查自定义页面目录是否有文件变化。不修改任何缓存。"""
    if _file_stats is None:
        return True
    return _get_file_stats(usrpages_dir()) != _file_stats


def _load_page_files(dir_path: Path, stats: dict):
    """扫描目录渲染全部页面，返回 (slug -> HTML, 失败快照)。

    单个文件失败只影响该文件（记日志并跳过），不会让整份页面缓存失效。
    """
    pages = {}
    failed = {}
    if not dir_path.exists():
        return pages, failed

    for md_file in dir_path.glob("*.evmd"):
        try:
            if md_file.stat().st_size > MAX_PAGE_SIZE:
                raise EvmdError(f"custom page file too large: {md_file.name}")
            content = md_file.read_text(encoding="utf-8")
            html = evmd_to_html(content)  # 自定义页面为纯正文，不带文档头
            pages[md_file.stem] = html    # 文件名去掉扩展名作为 slug
        except (EvmdError, OSError, UnicodeDecodeError, ValueError) as e:
            failed[md_file.name] = stats.get(md_file.name)
            logger.error("Failed to load custom page %s: %s", md_file.name, e)
    return pages, failed


def _rescan() -> None:
    """重新扫描目录并一次性提交快照与页面缓存。"""
    global _pages_cache, _file_stats
    dir_path = usrpages_dir()
    new_stats = _get_file_stats(dir_path)
    new_pages, _failed = _load_page_files(dir_path, new_stats)
    # 全部成功后再提交：不会出现"快照已更新、缓存还是旧的"的永久错位
    _file_stats = new_stats
    _pages_cache = new_pages


def get_page(slug: str):
    """获取自定义页面的 HTML；slug 非法或页面不存在时返回 None。"""
    if not validate_slug(slug):
        return None
    if _pages_cache is None or _has_changes():
        _rescan()
    return _pages_cache.get(slug)


def load_pages():
    """强制重新加载所有自定义页面（例如应用启动时调用）。"""
    _rescan()
