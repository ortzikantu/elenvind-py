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
- TOML 解析失败时把 tomllib 的原始错误一并带上，便于定位是哪一行写错。
"""
import tomllib


class EvmdError(ValueError):
    """EVMD 的结构/语法错误（缺文档头、未闭合、TOML 非法等）。"""


# 文档头标记：必须独占一行，前后不允许空格
OPEN_MARKER = "@@@\n"
CLOSE_MARKER = "\n@@@\n"


def split_document(text: str):
    """把整份文本拆成 (元数据字典 或 None, 正文字符串)。

    - 不以 "@@@" 开头：视为无文档头的纯正文，返回 (None, text)。
    - 以 "@@@" 开头但找不到闭合行：抛 EvmdError（文档头未闭合）。
    - 正常情况：返回 (元数据 dict, 去掉文档头后的正文)。
    """
    if not text.startswith(OPEN_MARKER):
        return None, text

    close = text.find(CLOSE_MARKER)
    if close == -1:
        raise EvmdError('document header is not closed: expected a line "@@@" after the header')

    # OPEN_MARKER 之后到闭合标记之前的区域即 TOML 文本
    toml_text = text[len(OPEN_MARKER):close]
    try:
        meta = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as e:
        raise EvmdError(f"invalid TOML header: {e}") from e

    # 跳过闭合标记（含其前后换行），剩余部分就是正文
    body = text[close + len(CLOSE_MARKER):]
    return meta, body
