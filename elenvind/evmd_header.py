"""EVMD 文档头解析：@@@ 包裹的 TOML 元数据块（俗称 front matter）。

语法（与 docs/EVMD_SPEC.md 保持一致）：

    @@@                       <- 第一行必须是独立的 "@@@"
    title = "Article Title"
    date  = 2024-01-01T10:00:00+08:00
    authors = ["Alice"]
    @@@                       <- 闭合行同样独占一行
    （正文从此开始）

设计要点：
- 允许"无文档头"的纯正文文档（自定义页面等场景直接写正文）。
- 文档头以 "@@@" 开头却未闭合时抛 EvmdError，方便上层精确报错。
- 换行统一按 `str.splitlines()` 的语义处理：CRLF / CR / LF 都能解析，
  末尾是否带换行也不影响闭合判定（Windows 上写出的文件不会莫名"没有文档头"）。
- TOML 解析失败时把 tomllib 的原始错误一并带上，便于定位是哪一行写错。
"""
import tomllib

MARKER = "@@@"


class EvmdError(ValueError):
    """EVMD 的结构/语法错误（缺文档头、未闭合、TOML 非法等）。"""


def normalize_line_endings(text: str) -> str:
    """把 CRLF / 裸 CR 统一成 LF，行尾与行首多余空白保持不变。"""
    if not isinstance(text, str):
        raise EvmdError(f"document must be a string, got {type(text).__name__}")
    if "\r" in text:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def split_document(text: str):
    """把整份文本拆成 (元数据字典 或 None, 正文字符串)。

    - 第一行不是独立 "@@@"：视为无文档头的纯正文，返回 (None, text)。
    - 第一行是 "@@@" 但找不到闭合行：抛 EvmdError（文档头未闭合）。
    - 正常情况：返回 (元数据 dict, 去掉文档头后的正文)。
    """
    text = normalize_line_endings(text)
    lines = text.splitlines()

    if not lines or lines[0] != MARKER:
        return None, text

    close = None
    for index in range(1, len(lines)):
        if lines[index] == MARKER:
            close = index
            break
    if close is None:
        raise EvmdError('document header is not closed: expected a line "@@@" after the header')

    toml_text = "\n".join(lines[1:close])
    try:
        meta = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as e:
        raise EvmdError(f"invalid TOML header: {e}") from e

    body = "\n".join(lines[close + 1:])
    return meta, body
