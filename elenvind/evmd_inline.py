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
- 先转义、后格式匹配：第 3 步之后正文里已不存在 `"` `<` `>` 等字符，
  因此格式规则提取出的 URL 不可能再逃出属性（第 4 步仍对 URL 显式转义，双保险）；
- URL 协议经 safe_url 限制为 http/https，并拒绝控制字符；
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

# 占位符清理：解析开始前先把用户输入里的私有区字符换成普通字符。
# 否则形如 "\ue1009\ue101" 的输入会被误当成内部占位符（序号越界 → 500，
# 或指向别人的代码片段 → 内容错位）。私有区字符在正常文档里没有语义。
# 保留 \ue001（块级引擎生成的硬换行占位符，_join_paragraph_lines 可能把它放进行内文本）。
# 替换目标用补充私有区 A 的 U+F0000，不会与任何生成中的占位符冲突。
_PUA_RE = re.compile("[\ue000-\ue000\ue002-\ue4ff]")
_PUA_REPLACEMENT = "\U000f0000"

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

# 单段行内文本的长度上限：超过则整段按纯文本转义输出。
# 正常文章段落远小于该值；上限只用于阻断"1 MB 全是反引号/占位符"这类
# 会造成二次方级占位符展开的恶意输入（见模块 docstring 的安全性说明）。
MAX_INLINE_LENGTH = 100_000

_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")


def escape_html(text: str) -> str:
    """转义 HTML 特殊字符（含引号）。"""
    return html.escape(str(text), quote=True)


def safe_url(url: str) -> str:
    """协议白名单校验：允许则返回原始 URL，否则返回空串。

    规则：
    - 先去掉首尾空白（浏览器解析 URL 时同样忽略首尾空白），
      再去掉空白后仍含空白/控制字符的一律拒绝（CRLF 与属性注入的原料）；
    - 协议必须在 http/https 白名单内，且必须存在主机名
      （"http://" 这种空壳不算合法地址）。
    """
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url or _CONTROL_RE.search(url):
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in SAFE_PROTOCOLS:
        return ""
    if not parsed.netloc:
        return ""
    return url


def parse_inline(text: str) -> str:
    """把行内 EVMD 语法解析为 HTML；入参为未转义的原始文本。"""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > MAX_INLINE_LENGTH:
        # 资源上限：超长段落不做格式解析，直接整体转义输出
        return escape_html(text)

    # 0) 清掉用户输入里的私有区字符，避免与内部占位符混淆
    text = _PUA_RE.sub(_PUA_REPLACEMENT, text)

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

    # 4) 依次应用格式规则（URL 在拼进 href 时再次转义）
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _STAR_ITALIC_RE.sub(r'<em>\1</em>', text)

    def _link_html(match):
        # 第 3 步已整体转义过，这里先还原成原始 URL 再统一转义，
        # 否则 URL 里的 & 会被二次转义成 &amp;amp;（历史缺陷）
        url = safe_url(html.unescape(match.group(1)))
        if not url:
            # 协议不在白名单：只保留链接文字，不产生可点击的 URL
            return match.group(2)
        return f'<a href="{escape_html(url)}">{match.group(2)}</a>'
    text = _LINK_RE.sub(_link_html, text)

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
        # 被转义的字符此时才还原，且必须再转义一次：\X 中的 X 可能是 < > & 等
        # HTML 元字符，直接还原等于把"转义"变成"注入原语"（历史漏洞，已修）。
        lambda m: escape_html(escaped_chars[int(m.group(1))]),
        text,
    )
    return text
