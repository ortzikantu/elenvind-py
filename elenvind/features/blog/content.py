"""内容格式：TOML 文档头 + Markdown 正文。

刻意**只有一层**：不发明标记语言，正文就是标准 Markdown，
元数据用标准 TOML。

    +++
    title = "Article Title"
    date = 2026-01-01T10:00:00+08:00
    lastmod = 2026-02-01T09:00:00+08:00     # 可选
    authors = ["Alice"]                     # 可选
    tags = ["python", "web"]                # 可选
    summary = "一句摘要"                     # 可选
    +++

    # 正文标题

    正文是**标准 Markdown**（表格 / 脚注 / 代码围栏由 core.markdown 的固定扩展集支持）。

设计取舍：
- 分隔符用 `+++` 而不是 `---`：Markdown 里 `---` 是分隔线/Setdown 标题，
  用它当 front matter 边界会和正文语法打架。`+++` 在 Markdown 里没有含义。
- 文档头本身仍是 TOML（标准库 tomllib 解析），不引入 YAML 依赖。
- 行结束符 CRLF / CR / LF 等价；末尾无换行也能闭合（Windows 编辑友好）。
"""
from __future__ import annotations

import re
import tomllib
from datetime import date, datetime, timezone

MARKER = "+++"
#: slug 白名单：[A-Za-z0-9._-]，禁止 "." / ".." / 点开头
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ContentError(ValueError):
    """内容文件结构/元数据错误（缺文档头、TOML 非法、字段类型不对等）。"""


def normalize_newlines(text: str) -> str:
    """CRLF / 裸 CR 统一成 LF。"""
    if not isinstance(text, str):
        raise ContentError(f"content must be a string, got {type(text).__name__}")
    if "\r" in text:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def parse_document(text: str):
    """把整份文件解析成 (metadata dict, body markdown)。

    - 第一行不是独立 `+++`：视为**无文档头**的纯 Markdown 正文（自定义页面用）。
    - 第一行是 `+++` 但找不到闭合行：抛 ContentError。
    - 文档头 TOML 非法：抛 ContentError（带 tomllib 的原始信息，便于定位）。
    """
    text = normalize_newlines(text)
    lines = text.splitlines()
    if not lines or lines[0].strip() != MARKER:
        return {}, text

    close = None
    for index in range(1, len(lines)):
        if lines[index].strip() == MARKER:
            close = index
            break
    if close is None:
        raise ContentError(f'front matter is not closed: expected a line "{MARKER}"')

    toml_text = "\n".join(lines[1:close])
    try:
        metadata = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise ContentError(f"invalid TOML front matter: {exc}") from exc

    body = "\n".join(lines[close + 1:])
    return metadata, body


def _as_text(value, field: str):
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise ContentError(f"field {field!r} must be a string, got {type(value).__name__}")


def _as_list(value, field: str):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            text = _as_text(item, field)
            if text:
                items.append(text)
        return items
    raise ContentError(f"field {field!r} must be a string or list of strings")


def normalize_metadata(metadata: dict, *, slug: str, require_header: bool):
    """校验并归一化元数据；缺失必需字段时抛 ContentError。

    返回的 dict 字段固定为：slug / title / date / lastmod / authors / tags /
    summary / source，全部为 str 或 list[str]（不向模板泄漏原始 TOML 类型）。
    """
    if require_header and not metadata:
        raise ContentError(
            f'front matter is required: the file must start with a line "{MARKER}"')

    title = _as_text(metadata.get("title"), "title")
    if require_header and not title:
        raise ContentError("front matter must define a non-empty 'title'")

    return {
        "slug": slug,
        "title": title or slug,
        "date": _as_text(metadata.get("date"), "date"),
        "lastmod": _as_text(metadata.get("lastmod"), "lastmod"),
        "authors": _as_list(metadata.get("authors"), "authors"),
        "tags": _as_list(metadata.get("tags"), "tags"),
        "summary": _as_text(metadata.get("summary"), "summary") or "",
        "source": _as_text(metadata.get("source"), "source") or "",
    }


def sort_key(metadata: dict):
    """按 date 倒序排序用的键：解析失败视为最早。

    **必须归一化到同一时区意识**：`datetime.fromisoformat` 对带偏移的写法
    （`2026-09-07T10:00:00+08:00`）返回 aware，对不带偏移的返回 naive，
    两者**不能比较**（`TypeError: can't compare offset-naive and offset-aware`）。
    而本 key 由 `articles.sort(...)` 使用，位置在 per-file try/except **之外**；
    只要有一篇日期风格不同（或干脆没写 date，此时返回 naive 的 datetime.min），
    排序就抛异常。`load_articles` 是启动钩子，等于整站起不来。

    处理：统一转成 **UTC aware**，无时区的按 UTC 解释。这样两种写法都能比较，
    且结果符合直觉（带偏移按真实时刻比较，不带偏移按 UTC 字面量比较）。
    """
    raw = metadata.get("date")
    if not raw:
        return _EPOCH_UTC
    try:
        value = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return _EPOCH_UTC
    return as_utc(value)


#: 无日期 / 解析失败时使用的排序键（aware，可与其余 key 比较）。
_EPOCH_UTC = datetime.min.replace(tzinfo=timezone.utc)


def as_utc(value: datetime) -> datetime:
    """把 datetime 归一化成 UTC aware；无时区的按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_slug(slug: str) -> bool:
    """slug 白名单校验；不合法一律拒绝（不进入查表，也不用于拼路径）。"""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return False
    return slug not in (".", "..") and not slug.startswith(".")
