"""Blog Feature：文章索引、正文渲染缓存、文章页与评论。

职责边界（只做业务）：
- 扫描配置目录、解析文档头、调用 `core.markdown.render_markdown` 渲染正文；
- 组装模板数据（分页、评论树、权限动作）；
- 返回 Core 的 Response。

**不做**：session / csrf / cookie / 安全头 / 请求校验 —— 全部由 Core 负责。

缓存策略（与 Pages 一致）：
- 索引缓存：缓存"元数据列表 + 目录文件状态快照"，按 (mtime, size) 比对；
- 原子提交：先扫描出 new_stats / new_articles，**全部成功**后才同时替换两个缓存，
  避免"快照已更新、索引仍旧"的永久错位；
- 正文缓存：读取前后各取一次 stat，两次一致才写缓存，
  避免把"读取过程中被改动"的内容永久钉住。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ...core.config import ROOT, config, resolve_path
from ...core.db_comment import (
    get_comment_by_id,
    get_comments_by_article,
    restore_comment,
    soft_delete_comment,
)
from ...core.db_comment_rate import try_post_comment
from ...core.markdown import render_markdown
from ...core.security import is_admin, is_admin_id
from ...core.utils import format_date
from .content import (
    ContentError,
    normalize_metadata,
    parse_document,
    sort_key,
)

logger = logging.getLogger(__name__)

#: 单文件上限（防止意外写入超大文件拖垮渲染）
MAX_ARTICLE_SIZE = 1 * 1024 * 1024
#: 单篇文章评论总数上限（可被 config 覆盖）
DEFAULT_MAX_COMMENTS_PER_ARTICLE = 1000
#: 评论默认层级上限
DEFAULT_MAX_DEPTH = 32

_articles_cache = None      # 元数据列表（新文章在前）；None 表示尚未扫描
_file_stats = None          # {文件名: (mtime, size)}
_body_cache = {}            # {绝对路径: (mtime, size, metadata, Markup)}
_failed_stats = {}          # 解析失败的快照，用于抑制重复报错


def articles_dir() -> Path:
    """文章目录：config.toml 顶层 articles_dir（相对项目根）。"""
    return resolve_path(config.get("articles_dir"), ROOT / "articles")


def _read_with_limit(file_path: Path) -> str:
    size = file_path.stat().st_size
    if size > MAX_ARTICLE_SIZE:
        raise ContentError(f"article file too large: {file_path.name} ({size} bytes)")
    return file_path.read_text(encoding="utf-8")


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


def _load_metadata(dir_path: Path, stats: dict):
    """扫描目录只解析文档头；单文件失败不影响整份索引。"""
    articles = []
    failed = {}
    if not dir_path.exists():
        return articles, failed

    for path in dir_path.glob("*.md"):
        snapshot = stats.get(path.name)
        try:
            source = _read_with_limit(path)
            metadata, _body = parse_document(source)
            articles.append(normalize_metadata(metadata, slug=path.stem,
                                               require_header=True))
        except (ContentError, OSError, UnicodeDecodeError, ValueError) as exc:
            failed[path.name] = snapshot
            if _failed_stats.get(path.name) != snapshot:
                logger.error("Failed to parse article %s: %s", path.name, exc)

    articles.sort(key=sort_key, reverse=True)
    return articles, failed


def _rescan() -> None:
    """重新扫描并**一次性**提交快照 + 索引 + 失败记录。"""
    global _articles_cache, _file_stats, _failed_stats
    directory = articles_dir()
    new_stats = _get_file_stats(directory)
    new_articles, new_failed = _load_metadata(directory, new_stats)
    _file_stats = new_stats
    _articles_cache = new_articles
    _failed_stats = new_failed


def _has_changes() -> bool:
    return _file_stats is None or _get_file_stats(articles_dir()) != _file_stats


def get_articles():
    """文章元数据列表（新文章在前），目录变化时自动重扫。"""
    if _articles_cache is None or _has_changes():
        logger.info("Article directory changed, rescanning index")
        _rescan()
    return _articles_cache


def get_article_by_slug(slug: str):
    if not slug:
        return None
    for article in get_articles():
        if article["slug"] == slug:
            return article
    return None


def load_article_body(slug: str, meta=None):
    """返回 (metadata, Markup body)；文章不存在返回 (None, None)。

    读取前后 stat 一致才写缓存；解析错误向上抛给视图层处理成错误页。
    """
    meta = meta or get_article_by_slug(slug)
    if not meta:
        return None, None

    file_path = articles_dir() / f"{meta['slug']}.md"
    before = file_path.stat()
    key = str(file_path)
    cached = _body_cache.get(key)
    if cached and cached[0] == before.st_mtime and cached[1] == before.st_size:
        return cached[2], cached[3]

    source = _read_with_limit(file_path)
    metadata, body = parse_document(source)
    normalized = normalize_metadata(metadata, slug=slug, require_header=True)
    rendered = render_markdown(body)
    try:
        after = file_path.stat()
    except OSError:
        return normalized, rendered
    if (after.st_mtime, after.st_size) == (before.st_mtime, before.st_size):
        _body_cache[key] = (before.st_mtime, before.st_size, normalized, rendered)
    else:
        logger.info("Article %s changed while rendering, cache not updated", slug)
    return normalized, rendered


def load_articles():
    """启动时强制重扫（由 lifespan 钩子调用）。"""
    _rescan()


# ======================= 模板数据组装 =======================

def article_list_context(page: int):
    """首页文章列表上下文（分页数据在 Feature 里算好）。"""
    per_page = config.get("pagination", {}).get("per_page", 10)
    if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page <= 0:
        per_page = 10
    articles = get_articles()
    total = len(articles)
    total_pages = (total + per_page - 1) // per_page if total else 0
    page = max(1, min(page, total_pages) if total_pages else 1)
    window = articles[(page - 1) * per_page:page * per_page]
    rows = [{
        "slug": article["slug"],
        "title": article["title"],
        "date_display": format_date(article.get("date")),
        "date_attr": article.get("date") or "",
    } for article in window]
    return {
        "articles": rows,
        "page": page,
        "total_pages": total_pages,
        "total_articles": total,
        "hero_url": (config.get("static") or {}).get("hero") or "",
        "intro": str((config.get("params") or {}).get("intro") or "").strip(),
    }


def article_context(slug: str):
    """文章页上下文；文章不存在返回 None。"""
    meta = get_article_by_slug(slug)
    if meta is None:
        return None
    metadata, body = load_article_body(slug, meta)
    authors = ", ".join(metadata["authors"]) if metadata["authors"] else ""
    return {
        "article": {
            "slug": slug,
            "title": metadata["title"] or meta["title"],
            "date_display": format_date(metadata.get("date")),
            "lastmod_display": format_date(metadata.get("lastmod")) if metadata.get("lastmod") else "",
            "authors_text": authors,
            "body": body,
        },
    }


# ======================= 评论 =======================

def _max_depth() -> int:
    value = config.get("max_comment_depth", DEFAULT_MAX_DEPTH)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_DEPTH


def _comment_limits():
    raw = config.get("comment_limits") or {}
    limits = {"max_per_user": 5, "max_per_ip": 10, "window_seconds": 60}
    if isinstance(raw, dict):
        for key in limits:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


def _max_comments_per_article() -> int:
    value = config.get("max_comments_per_article", DEFAULT_MAX_COMMENTS_PER_ARTICLE)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_COMMENTS_PER_ARTICLE


def build_comment_rows(slug: str, user, *, max_length: int):
    """把评论行组装成模板可直接渲染的结构（权限判断在 Feature 里完成）。

    评论树用 children_by_parent + 显式栈迭代展开（O(n)，无递归）。
    """
    comments = get_comments_by_article(slug)
    by_id = {row["id"]: row for row in comments}
    children = {}
    roots = []
    for row in comments:
        parent_id = row["parent_id"]
        if parent_id is None or parent_id not in by_id:
            roots.append(row)
        else:
            children.setdefault(parent_id, []).append(row)

    ordered = []
    stack = [(row, 1) for row in reversed(roots)]
    while stack:
        row, depth = stack.pop()
        ordered.append((row, depth))
        for child in reversed(children.get(row["id"], ())):
            stack.append((child, depth + 1))

    admin = is_admin(user)
    deleted_nickname = config.get("deleted_user_nickname", "Journeyed On")
    badge_text = str(config.get("admin_badge", "BIG BOSS")).strip()
    depth_limit = _max_depth()
    rows = []

    for row, depth in ordered:
        parent = by_id.get(row["parent_id"])
        author = _author_label(row, deleted_nickname)
        badge = badge_text if (badge_text and is_admin_id(row["user_id"])) else ""
        actions = []
        if user is not None:
            is_owner = user["id"] == row["user_id"]
            if not row["is_deleted"]:
                if admin or is_owner:
                    actions.append({"url": f"/article/{slug}/comment/delete/{row['id']}",
                                    "action": "delete", "css": "delete-link"})
            elif admin:
                actions.append({"url": f"/article/{slug}/comment/restore/{row['id']}",
                                "action": "restore", "css": "restore-link"})
        reply_url = (f"/article/{slug}?reply_to={row['id']}#comments"
                     if user is not None and depth < depth_limit else "")

        rows.append({
            "id": row["id"],
            "depth": depth,
            "author": author,
            "badge": badge,
            "created": row["created_at"],
            "is_deleted": bool(row["is_deleted"]),
            "content": _comment_content(row, admin),
            "reply_to": _reply_label(parent, deleted_nickname) if parent else "",
            "actions": actions,
            "reply_url": reply_url,
        })
    return rows, len(comments), max_length


def _author_label(row, deleted_nickname: str) -> str:
    if row["user_deleted"]:
        return f"{deleted_nickname} (#{row['user_id']})"
    return f"{row['nickname'] or 'Unknown'} (#{row['user_id']})"


def _reply_label(parent, deleted_nickname: str) -> str:
    return f"Replying to @{_author_label(parent, deleted_nickname)}:"


def _comment_content(row, admin: bool):
    """评论正文：已删除对访客打码，对管理员原文加删除线。返回 Markup。"""
    from markupsafe import Markup

    from ...core.utils import escape_html

    raw = row["content"]
    if row["is_deleted"] and not admin:
        block_len = len(raw.replace("\r\n", "\n").replace("\r", "\n"))
        return Markup(f'<span class="comment-content">{"█" * block_len}</span>')
    text = escape_html(raw).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    css = "comment-content is-deleted" if row["is_deleted"] else "comment-content"
    return Markup(f'<span class="{css}">{text}</span>')


def comment_depth(comment) -> int:
    """沿 parent_id 回溯算层级；迭代 + 访问集合，数据成环也不死循环。"""
    depth = 1
    parent_id = comment["parent_id"]
    seen = {comment["id"]}
    limit = _max_depth() + 1
    while parent_id is not None and depth < limit:
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = get_comment_by_id(parent_id)
        if parent is None:
            break
        depth += 1
        parent_id = parent["parent_id"]
    return depth


def post_comment(slug: str, user_id: int, client_ip: str, content: str, reply_to):
    """写入评论（限流与插入在同一事务内）。返回 (outcome, message)。"""
    limits = _comment_limits()
    content = content.strip()
    try:
        max_length = int(config.get("max_length", 1000))
    except (TypeError, ValueError):
        max_length = 1000
    if not content:
        return "empty", "Content cannot be empty"
    if len(content) > max_length:
        return "too_long", "Content too long"

    outcome = try_post_comment(
        slug, user_id, client_ip, content, parent_id=reply_to,
        max_per_user=limits["max_per_user"], max_per_ip=limits["max_per_ip"],
        window_seconds=limits["window_seconds"],
        max_per_article=_max_comments_per_article(),
        created_at=datetime.now().isoformat(),
    )
    messages = {
        "rate_user": "Too many comments. Please slow down.",
        "rate_ip": "Too many comments from this address. Please slow down.",
        "too_many": "This article has reached the comment limit.",
    }
    return outcome, messages.get(outcome, "")


def remove_comment(comment_id: int):
    soft_delete_comment(comment_id)


def restore_comment_by_id(comment_id: int):
    restore_comment(comment_id)


def default_max_length() -> int:
    try:
        value = int(config.get("max_length", 1000))
        return value if value > 0 else 1000
    except (TypeError, ValueError):
        return 1000
