"""文章索引与正文加载（内容为 .evmd 文件，格式见 docs/EVMD_SPEC.md）。

缓存策略（保持简单、适合个人站规模）：
- 索引缓存：只缓存"元数据列表 + 目录文件状态快照"，每次请求比对 (mtime, size)
  快照，目录有变化才重新扫描，因此新增/修改/删除文章无需重启。
- 正文缓存：文章页访问时若文件 (mtime, size) 未变化，直接复用上次的
  （元数据, HTML）渲染结果，避免每次请求都重复做 TOML 解析与 EVMD 渲染；
  文件一变即自动失效重渲，内容永远最新。
- 单文件大小上限 MAX_ARTICLE_SIZE 防止意外写入超大文件拖垮渲染。
"""
import logging
from datetime import datetime
from pathlib import Path

from .evmd_parser import document_meta, parse_document, EvmdError

logger = logging.getLogger(__name__)

# 文章目录位于项目根 /articles；文件扩展名 .evmd
ARTICLES_DIR = Path(__file__).resolve().parent.parent / "articles"
MAX_ARTICLE_SIZE = 1 * 1024 * 1024  # 1 MB

_articles_cache = []          # 文章元数据列表（新文章在前）
_file_stats = {}              # 上次扫描时的文件状态快照 {文件名: (mtime, size)}
_body_cache = {}              # 正文渲染缓存 {文件路径: (mtime, size, 元数据, HTML)}


def _read_with_limit(file_path: Path) -> str:
    """读取文件并做大小限制，超限抛出带文件名的 EvmdError。"""
    size = file_path.stat().st_size
    if size > MAX_ARTICLE_SIZE:
        raise EvmdError(f"article file too large: {file_path.name} ({size} bytes)")
    return file_path.read_text(encoding="utf-8")


def _get_file_stats(dir_path: Path):
    """获取目录下所有 .evmd 文件的状态字典。"""
    stats = {}
    if dir_path.exists():
        for f in dir_path.glob("*.evmd"):
            try:
                st = f.stat()
                stats[f.name] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    return stats


def _has_changes() -> bool:
    """检查文章目录是否有文件变化（新增、删除、修改）。"""
    global _file_stats
    current_stats = _get_file_stats(ARTICLES_DIR)
    if current_stats != _file_stats:
        _file_stats = current_stats
        return True
    return False


def _load_article_metadata():
    """扫描目录，加载所有文章元数据（只解析文档头，不渲染正文）。"""
    metadata_list = []
    if not ARTICLES_DIR.exists():
        return metadata_list

    for md_file in ARTICLES_DIR.glob("*.evmd"):
        try:
            content = _read_with_limit(md_file)
            meta = document_meta(content, require_header=True)
            slug = md_file.stem
            metadata_list.append({
                "slug": slug,
                "title": meta.get("title", slug),
                "date": meta.get("date"),
                "lastmod": meta.get("lastmod"),
                "authors": meta.get("authors", []),
                "source": meta.get("source", ""),
                "file_path": str(md_file),
            })
        except Exception as e:
            logger.error(f"Failed to parse article {md_file.name}: {e}")

    # 按 date 倒序排序：TOML 内联日期会被 tomllib 解析成 datetime，直接可比；
    # 兜底尝试 fromisoformat，仍失败则视为最早（datetime.min）
    def sort_key(article):
        date = article["date"]
        if isinstance(date, datetime):
            return date
        try:
            return datetime.fromisoformat(str(date))
        except Exception:
            return datetime.min

    metadata_list.sort(key=sort_key, reverse=True)
    return metadata_list


def get_articles():
    """获取文章元数据列表，目录有变化时自动重新扫描。"""
    global _articles_cache
    if _has_changes() or _articles_cache is None:
        logger.info("Article directory changed, rescanning index")
        _articles_cache = _load_article_metadata()
    return _articles_cache


def get_article_by_slug(slug: str):
    """根据 slug 查文章元数据（不加载正文）。"""
    for article in get_articles():
        if article["slug"] == slug:
            return article
    return None


def load_article_content(slug: str):
    """加载文章并渲染，返回 (元数据 dict, 正文 HTML)；文章不存在返回 (None, None)。

    渲染结果按文件 (mtime, size) 缓存：文件未变直接复用，变了才重新解析；
    解析/渲染失败时重新抛出异常，由上层视图层处理为 500 页。
    """
    article_meta = get_article_by_slug(slug)
    if not article_meta:
        return None, None

    file_path = Path(article_meta["file_path"])
    key = str(file_path)
    st = file_path.stat()
    cached = _body_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2], cached[3]

    content = _read_with_limit(file_path)
    meta, html = parse_document(content, require_header=True)
    _body_cache[key] = (st.st_mtime, st.st_size, meta, html)
    return meta, html


def load_articles():
    """强制重新扫描文章目录，更新缓存（例如应用启动时调用）。"""
    global _articles_cache, _file_stats
    logger.info("Manual article scan triggered")
    _file_stats = _get_file_stats(ARTICLES_DIR)  # 更新快照
    _articles_cache = _load_article_metadata()
    return _articles_cache
