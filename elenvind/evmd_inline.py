"""EVMD 行内语法解析：转义、`code`、**bold**、*italic*、_italic_、
~~deleted~~、@{link,...}、@{ref,...}。

解析流水线（按顺序，各步之间用私有区占位符隔离，互不干扰）：
1. 保护代码跨度 `` `...` ``：内容原样保留（含 $、\\、* 等），最后单独转义输出；
2. 反斜杠转义：\\X 中 X 暂存，正文换成占位符（从而绕过后面的格式规则）；
3. 整体 HTML 转义：任何原始 HTML / 引号都变成惰性文本；
4. 依次应用：**bold** -> *italic* -> @{link} -> _italic_ -> ~~del~~ -> @{ref}；
   （link 之后再处理 _，避免破坏 URL 里的下划线）
5. 还原占位符：代码（内容此时才转义）-> 转义字符字面量。

安全性原理：
- 先转义、后匹配，提取出的 URL/文本组无法再注入 HTML；
- URL 协议经 safe_url 限制为 http/https；
- 代码跨度全程以占位符隔离，不会被其它规则误改。
"""
import html
import re
from urllib.parse import urlparse

# 允许的反斜杠转义字符集（CommonMark 常用集合 + 本站方言符号）
_ESCAPABLE = "\\`*_{}[]()#+-.!~<>$|&^"
_ESCAPE_RE = re.compile(r"\\([" + re.escape(_ESCAPABLE) + r"])")

# 私有区占位符
_ESC_OPEN = "\ue400"      # 转义字符暂存：\ue400{序号}\ue401（最后还原为字面字符）
_ESC_CLOSE = "\ue401"
_CODE_OPEN = "\ue100"     # 代码跨度：\ue100{序号}\ue101
_CODE_CLOSE = "\ue101"
_REF_OPEN = "\ue300"      # 脚注引用：\ue300{id}\ue301（由块级引擎统一解析编号）
_REF_CLOSE = "\ue301"

# 行内语法规则
_CODE_RE = re.compile(r'`([^`]+)`')
_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')
_STAR_ITALIC_RE = re.compile(r'\*([^*]+)\*')
_LINK_RE = re.compile(r'@\{link,\s*([^,}]+?)\s*,\s*([^}]+?)\s*\}')
# 下划线斜体：仅当两侧不是拉丁字母/数字/下划线时生效（保护 URL 与单词内下划线）
_UNDER_ITALIC_RE = re.compile(r'(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])')
_DEL_RE = re.compile(r'~~([^~]+)~~')
_REF_RE = re.compile(r'@\{ref,\s*([A-Za-z0-9_-]{1,32})\s*\}')

# 内容中允许出现的 URL 协议白名单
SAFE_PROTOCOLS = {"http", "https"}


def escape_html(text: str) -> str:
    """转义 HTML 特殊字符（含引号）。"""
    return html.escape(text, quote=True)


def safe_url(url: str) -> str:
    """协议白名单校验：允许则返回原 URL，否则返回空串。"""
    parsed = urlparse(url)
    if parsed.scheme in SAFE_PROTOCOLS:
        return url
    return ""


def parse_inline(text: str) -> str:
    """把行内 EVMD 语法解析为 HTML；入参为未转义的原始文本。"""
    # 1) 保护代码跨度（内容原样，包括 $ \ * 等）
    code_parts = []

    def _hold_code(match):
        index = len(code_parts)
        code_parts.append(match.group(1))
        return f"{_CODE_OPEN}{index}{_CODE_CLOSE}"
    text = _CODE_RE.sub(_hold_code, text)

    # 2) 反斜杠转义：被转义字符暂存入列表，正文换成占位符（格式规则看不到它）
    escaped_chars = []

    def _hold_escape(match):
        index = len(escaped_chars)
        escaped_chars.append(match.group(1))
        return f"{_ESC_OPEN}{index}{_ESC_CLOSE}"
    text = _ESCAPE_RE.sub(_hold_escape, text)

    # 3) 整体 HTML 转义
    text = escape_html(text)

    # 4) 依次应用格式规则
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _STAR_ITALIC_RE.sub(r'<em>\1</em>', text)
    text = _LINK_RE.sub(
        lambda m: f'<a href="{safe_url(m.group(1))}">{m.group(2)}</a>', text)
    text = _UNDER_ITALIC_RE.sub(r'<em>\1</em>', text)
    text = _DEL_RE.sub(r'<del>\1</del>', text)
    text = _REF_RE.sub(lambda m: f"{_REF_OPEN}{m.group(1)}{_REF_CLOSE}", text)

    # 5) 还原占位符：代码（内容此时才转义）-> 转义字符字面量
    text = re.sub(
        f"{_CODE_OPEN}(\\d+){_CODE_CLOSE}",
        lambda m: f"<code>{escape_html(code_parts[int(m.group(1))])}</code>",
        text,
    )
    text = re.sub(
        f"{_ESC_OPEN}(\\d+){_ESC_CLOSE}",
        lambda m: escaped_chars[int(m.group(1))],
        text,
    )
    return text
