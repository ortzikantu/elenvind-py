"""Blog 模块：文章索引、正文渲染缓存、文章页与评论。

职责边界（只做业务）：
- 扫描配置目录、解析文档头、调用 `core.markdown.render_markdown` 渲染正文；
- 组装模板数据（分页、评论树、权限动作）；
- 返回 Core 的 Response。

**不做**：session / csrf / cookie / 安全头 / 请求校验 —— 全部由 Core 负责。

依赖：只依赖 `core/`（配置、内容格式、Markdown、数据库模块、安全原语）。
对本项目其它业务模块**零依赖**；需要给别的模块提供数据时，暴露这里的公开函数
（如 `sitemap_articles()`），由装配层（`elenvind/app.py`）注入。

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

from markupsafe import Markup

from ...core.config import ROOT, config, resolve_path
from ...core.content import (
    ContentError,
    normalize_metadata,
    parse_document,
    sort_key,
)
from ...core.db_comment import (
    get_comment_by_id,
    get_comments_by_article,
    restore_comment,
    soft_delete_comment,
)
from ...core.db_comment import (
    comment_depth as _core_comment_depth,
)
from ...core.db_comment import (
    flatten_comment_tree,
)
from ...core.db_comment_rate import try_post_comment
from ...core.markdown import render_markdown
from ...core.security import is_admin, is_admin_id
from ...core.utils import escape_html, format_date

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

#: 正文缓存的条目上限。超出时先淘汰"已不在当前文章目录里"的条目
#: （重命名/删除留下的死键），仍然超出才按 mtime 淘汰最旧的。
#: 为什么需要：缓存键是**绝对路径**，重命名或重写文章会留下永远不再命中的
#: 旧键，而渲染后的 Markup 体积不小 —— 长时间运行会稳定泄漏内存。
BODY_CACHE_MAX_ENTRIES = 256


def _evict_body_cache(keep_paths=None):
    """把正文缓存压回上限内。

    `keep_paths` 给出"当前仍然存在的文件路径"；不属于它的条目是死键，
    优先淘汰（它们永远不会再被命中）。剩余按 mtime 从旧到新淘汰。
    """
    if len(_body_cache) <= BODY_CACHE_MAX_ENTRIES:
        # 顺手清理死键，避免它们一直占位
        if keep_paths is not None:
            for key in [k for k in _body_cache if k not in keep_paths]:
                del _body_cache[key]
        return
    if keep_paths is not None:
        for key in [k for k in _body_cache if k not in keep_paths]:
            del _body_cache[key]
    if len(_body_cache) <= BODY_CACHE_MAX_ENTRIES:
        return
    # 仍然超限：按 (mtime, size) 排序淘汰最旧的，直到回到上限
    stale = sorted(_body_cache.items(), key=lambda item: (item[1][0], item[1][1]))
    for key, _value in stale[:len(_body_cache) - BODY_CACHE_MAX_ENTRIES]:
        del _body_cache[key]


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
    # 索引刚刚重算过，正好知道"当前真实存在的文件"，借机淘汰正文缓存里的死键
    _evict_body_cache({str(directory / name) for name in new_stats})


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
        _evict_body_cache()
    else:
        logger.info("Article %s changed while rendering, cache not updated", slug)
    return normalized, rendered


def load_articles():
    """启动时强制重扫（由 lifespan 钩子调用）。"""
    _rescan()


def sitemap_articles():
    """`/sitemap.xml` 的公开数据源：`[(slug, 最后修改时间或 None), ...]`。

    由装配层（`elenvind/app.py`）注入给 seo 模块 —— seo 只负责把数据组装成
    XML，不 import 本模块的内部实现（模块之间互不 import）。
    返回的日期交给 `core.utils.format_date()` 格式化，与页面显示同一套规则。
    """
    return [(article["slug"], article.get("lastmod") or article.get("date"))
            for article in get_articles()]


# ======================= 模板数据组装 =======================

def article_list_context(page: int):
    """首页文章列表上下文（分页数据在模块里算好）。"""
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
    """把评论行组装成模板可直接渲染的结构（权限判断在模块里完成）。

    树展开委托 `core.db_comment.flatten_comment_tree` —— 那里是唯一定义，
    带防环与"不可达评论补根"处理（否则坏数据里的环会让评论从页面上消失）。
    """
    comments = get_comments_by_article(slug)
    by_id = {row["id"]: row for row in comments}
    ordered = flatten_comment_tree(comments)

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


def _normalize_newlines(text: str) -> str:
    """把 CRLF / 单独 CR 统一成 LF。

    正文会以 `<br>` 的形式展示，换行风格不统一会让行数计算（打码方块数）
    与实际渲染结果对不上，所以两个分支都必须先规范化再处理。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ⚠️ 此处是全项目唯一的原始 HTML 注入点，改动前务必确认转义顺序 ⚠️
#
# 与文章正文不同：评论**不走** `core.markdown` 的白名单净化器
# （评论是纯文本，不解析 Markdown），因此这里的转义是唯一防线。
# 顺序必须是「先 escape_html，再替换换行」：
#   - 先转义 -> `\n` 不受影响，可以安全换成 `<br>`
#   - 先换行 -> `<br>` 会被后面的转义变成 `&lt;br&gt;`，页面上就会看到字面量
# 任何时候都不要把 Markup() 用在未转义的 `row["content"]` 上。
def _comment_content(row, admin: bool):
    """评论正文：已删除对访客打码，对管理员原文加删除线。返回 Markup。"""
    raw = row["content"]
    if row["is_deleted"] and not admin:
        block_len = len(_normalize_newlines(raw))
        return Markup(f'<span class="comment-content">{"█" * block_len}</span>')
    text = escape_html(raw)
    text = _normalize_newlines(text).replace("\n", "<br>")
    css = "comment-content is-deleted" if row["is_deleted"] else "comment-content"
    return Markup(f'<span class="{css}">{text}</span>')


def comment_depth(comment) -> int:
    """沿 parent_id 回溯算层级（顶层 = 1）。

    实现在 `core.db_comment.comment_depth`——那是**层级计算的唯一定义**，
    写入侧的 `try_post_comment` 也调用它（在事务内校验深度），
    因此渲染与写入不可能算出不同的层级。这里只是按模块的习惯
    补上配置里的 max_depth 并转发，保持既有调用点与测试不变。

    防环（访问集合）与 limit 兜底都在那个实现里，未改动。
    """
    return _core_comment_depth(comment, max_depth=_max_depth())


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
        max_depth=_max_depth(),
    )
    messages = {
        "rate_user": "Too many comments. Please slow down.",
        "rate_ip": "Too many comments from this address. Please slow down.",
        "too_many": "This article has reached the comment limit.",
        # 父评论不存在或不属于本文：可能是手动改了表单，或原评论已被清掉
        "bad_parent": "The comment you are replying to no longer exists.",
        # 回复层级已达上限：模板不再显示回复按钮，但 reply_to 可以手工构造
        "too_deep": "Replies to this comment have reached the maximum depth.",
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
