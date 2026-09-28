"""文章索引与正文加载（内容为 .evmd 文件，格式见 docs/EVMD_SPEC.md）。

缓存策略（保持简单、适合个人站规模）：
- 索引缓存：只缓存“元数据列表 + 目录文件状态快照”，每次请求比对 (mtime, size)
  快照，目录有变化才重新扫描，因此新增/修改/删除文章无需重启。
- 快照与索引一次性提交：先扫描出 new_stats 与 new_articles，全部成功后才同时
  替换两个缓存。避免出现“快照已更新、索引仍是旧的”这种永久性错位。
- 正文缓存：文章页访问时若文件 (mtime, size) 未变化，直接复用上次的
  （元数据, HTML）渲染结果；读取前后各取一次文件状态，两次一致才写入缓存，
  防止把“读取过程中被改动”的内容永久钉在缓存里。
- 单文件大小上限 MAX_ARTICLE_SIZE 防止意外写入超大文件拖垮渲染。

路径安全：文章只来自配置的 articles 目录中 `*.evmd` 的**文件名**（stem），
URL 里的 slug 只做字典查表，从不参与任何文件路径拼接。
"""
import logging
from datetime import datetime
from pathlib import Path

from .config import config, ROOT, resolve_path
from .evmd_parser import document_meta, parse_document, EvmdError

logger = logging.getLogger(__name__)

MAX_ARTICLE_SIZE = 1 * 1024 * 1024  # 1 MB

_articles_cache = None        # 文章元数据列表（新文章在前）；None 表示尚未扫描
_file_stats = None            # 上次扫描时的文件状态快照 {文件名: (mtime, size)}
_body_cache = {}              # 正文渲染缓存 {文件路径: (mtime, size, 元数据, HTML)}
_failed_stats = {}            # 解析失败的快照 {文件名: (mtime, size)}，用于抑制重复报错


def articles_dir() -> Path:
    """文章目录：config.toml 顶层 articles_dir（相对项目根），默认 articles/。"""
    value = config.get("articles_dir", "articles")
    return resolve_path(value, ROOT / "articles")


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
                continue   # 扫描过程中文件被删除/重命名：跳过，下个请求会重扫
    return stats


def _load_article_metadata(dir_path: Path, stats: dict):
    """扫描目录，加载所有文章元数据（只解析文档头，不渲染正文）。

    单个文件失败只影响该文件（记日志并跳过），不会让整份索引失效；
    同一文件在内容未变化时的重复失败只报一次日志。
    返回 (元数据列表, 本轮失败快照)。
    """
    metadata_list = []
    new_failed = {}
    if not dir_path.exists():
        return metadata_list, new_failed

    for md_file in dir_path.glob("*.evmd"):
        snapshot = stats.get(md_file.name)
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
        except (EvmdError, OSError, UnicodeDecodeError, ValueError) as e:
            new_failed[md_file.name] = snapshot
            if _failed_stats.get(md_file.name) != snapshot:
                logger.error("Failed to parse article %s: %s", md_file.name, e)

    # 按 date 倒序排序：TOML 内联日期会被 tomllib 解析成 datetime，直接可比；
    # 兜底尝试 fromisoformat，仍失败则视为最早（datetime.min）
    def sort_key(article):
        date = article["date"]
        if isinstance(date, datetime):
            return date
        try:
            return datetime.fromisoformat(str(date))
        except (TypeError, ValueError):
            return datetime.min

    metadata_list.sort(key=sort_key, reverse=True)
    return metadata_list, new_failed


def _rescan() -> None:
    """重新扫描目录并**一次性**提交快照 + 索引 + 失败记录。"""
    global _articles_cache, _file_stats, _failed_stats
    dir_path = articles_dir()
    new_stats = _get_file_stats(dir_path)
    new_articles, new_failed = _load_article_metadata(dir_path, new_stats)
    # 全部成功后再提交：任何一步抛异常都不会留下半新半旧的状态
    _file_stats = new_stats
    _articles_cache = new_articles
    _failed_stats = new_failed


def _has_changes() -> bool:
    """检查文章目录是否有文件变化（新增、删除、修改）。不修改任何缓存。"""
    if _file_stats is None:
        return True
    return _get_file_stats(articles_dir()) != _file_stats


def get_articles():
    """获取文章元数据列表，目录有变化时自动重新扫描。"""
    if _articles_cache is None or _has_changes():
        logger.info("Article directory changed, rescanning index")
        _rescan()
    return _articles_cache


def get_article_by_slug(slug: str):
    """根据 slug 查文章元数据（不加载正文）。slug 只做查表，不拼路径。"""
    if not slug:
        return None
    for article in get_articles():
        if article["slug"] == slug:
            return article
    return None


def load_article_content(slug: str):
    """加载文章并渲染，返回 (元数据 dict, 正文 HTML)；文章不存在返回 (None, None)。

    渲染结果按文件 (mtime, size) 缓存：文件未变直接复用，变了才重新解析。
    读取前后各取一次文件状态，两次一致才写缓存——避免把读取过程中被替换的
    内容永久缓存（个人站不做文件锁，但可以保证"最终一定收敛到正确内容"）。

    文件级错误（语法错误 EvmdError、文件消失 OSError、编码损坏 UnicodeDecodeError）
    向上抛出，由视图层渲染错误提示；这些路径都不会污染缓存。
    """
    article_meta = get_article_by_slug(slug)
    if not article_meta:
        return None, None

    file_path = Path(article_meta["file_path"])
    key = str(file_path)
    before = file_path.stat()
    cached = _body_cache.get(key)
    if cached and cached[0] == before.st_mtime and cached[1] == before.st_size:
        return cached[2], cached[3]

    content = _read_with_limit(file_path)
    meta, html = parse_document(content, require_header=True)
    try:
        after = file_path.stat()
    except OSError:
        # 读取完成但文件已被删除：本次结果仍然可用，只是不写缓存
        return meta, html
    if (after.st_mtime, after.st_size) == (before.st_mtime, before.st_size):
        _body_cache[key] = (before.st_mtime, before.st_size, meta, html)
    else:
        logger.info("Article %s changed while rendering, cache not updated", slug)
    return meta, html


def load_articles():
    """强制重新扫描文章目录，更新缓存（例如应用启动时调用）。"""
    logger.info("Manual article scan triggered")
    _rescan()
    return _articles_cache
