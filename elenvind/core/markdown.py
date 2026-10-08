"""Core Markdown：Markdown 的唯一入口 + 输出净化。

    模块 ──► render_markdown(text) ──► python-markdown ──► 白名单净化 ──► Markup

**为什么必须净化（实测结论，不是理论担忧）**：python-markdown 默认
*原样透传*原始 HTML，并且接受危险协议：

    <script>alert(1)</script>   ->  <script>alert(1)</script>
    ![i](javascript:alert(1))   ->  <img src="javascript:alert(1)">
    [x](javascript:alert(1))    ->  <a href="javascript:alert(1)">x</a>

渲染结果要作为"受信 HTML"交给 Jinja（`Markup`），所以必须先净化，
否则等于给内容作者一个存储型 XSS 入口。

流水线（三步，缺一不可）：
1. **围栏代码外**的裸 HTML 标签先转义（代码块内不动，交给 Markdown 自己转义，
   避免 `&lt;` 被二次转义成 `&amp;lt;`）；
2. python-markdown 渲染（固定扩展集）；
3. 白名单净化：只保留白名单标签/属性；不在白名单里的标签**转义为可见文本**
   （而不是静默丢弃，避免内容"凭空消失"）；URL 属性做协议白名单。

净化器只用标准库 `html.parser`，不引入 bleach 之类的第三方依赖。
"""
import re
from html import escape as html_escape
from html.parser import HTMLParser

from markdown import Markdown
from markdown.extensions import Extension
from markdown.postprocessors import Postprocessor
from markdown.preprocessors import Preprocessor
from markupsafe import Markup

#: 固定扩展集：够个人站用，且不引入额外第三方依赖。
#: 刻意不含 `smarty`（会把直引号变弯引号，属于对用户文本的意外改写）。
_EXTENSIONS = [
    "extra",          # tables / fenced_code / attr_list / footnotes / def_list 等
    "sane_lists",     # 列表行为更可预测
    "toc",            # 目录锚点（模板可选使用）
]
_EXTENSION_CONFIGS = {"toc": {"permalink": False, "anchorlink": False}}

#: 允许保留的标签（python-markdown 常规产出 + 排版所需）
ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "em", "strong", "del", "ins", "sub", "sup", "mark", "small",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "pre", "code", "span", "div",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "a", "img",
    "abbr", "cite", "q", "kbd", "samp", "var", "time",
    "figure", "figcaption", "video", "source",
})

#: 连同内容一起删除的标签（模板自身不会输出；出现在内容里说明是攻击或误用）
DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "object", "embed", "template", "noscript",
    "svg", "math", "form", "input", "button", "textarea", "select", "option",
})

#: 允许保留的属性（按标签细分；全局属性单独放行）
GLOBAL_ATTRS = frozenset({
    "class", "id", "title", "lang", "dir",
    "data-footnote-ref", "data-footnote-backref", "aria-label", "aria-hidden", "role",
})
TAG_ATTRS = {
    "a": frozenset({"href", "rel", "target", "name"}),
    "img": frozenset({"src", "alt", "width", "height", "loading"}),
    "video": frozenset({"src", "poster", "controls", "width", "height",
                        "preload", "muted", "loop"}),
    "source": frozenset({"src", "type", "media"}),
    "td": frozenset({"colspan", "rowspan", "align"}),
    "th": frozenset({"colspan", "rowspan", "align", "scope"}),
    "col": frozenset({"span"}),
    "ol": frozenset({"start", "type"}),
    "time": frozenset({"datetime"}),
    "li": frozenset({"value"}),
}

#: URL 属性只允许这些协议（javascript:/data:/vbscript:/file: 一律丢弃该属性）
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})
_URL_ATTRS = frozenset({"href", "src", "poster", "action", "formaction", "cite"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_VOID_TAGS = frozenset({"br", "hr", "img", "col", "source", "area", "base", "link", "meta"})
#: 代码围栏（``` 或 ~~~），用于跳过"围栏内不做预转义"的判断
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

# ---------------------------------------------------------------------------
# @video(url) 指令
# ---------------------------------------------------------------------------
#: 正文里的视频指令：`@video(https://example.com/clip.mp4)`
#: 刻意做成显式语法而不是"裸 HTML `<video>`"——后者会被预转义成可见文本
#: （这是内容安全策略，不打算为了视频破例）。指令只在**行首**生效，
#: 与 Markdown 的块级语法一致。
VIDEO_MARKER = "@video("
VIDEO_RE = re.compile(r"^[ \t]*@video\(\s*(?P<url>[^()\s]+)\s*\)[ \t]*$", re.MULTILINE)

#: 允许出现在视频 src 里的协议（与链接/图片同一套白名单，只是不含 mailto）
VIDEO_SCHEMES = frozenset({"http", "https"})

#: 占位符用 Unicode 私用区字符包起来：正常内容不会包含它们，
#: 且 Markdown 不会把 % 或字母数字之外的字符当成实体处理，因此能安全穿过渲染。
_SENTINEL_OPEN = "\ue000ELENVIND-VIDEO:"
_SENTINEL_CLOSE = "\ue001"


def safe_media_url(value: str):
    """校验视频/媒体地址：允许 http(s) 与站内路径，其余返回 None。

    与链接白名单一致的思路——`javascript:`、`data:` 之类一律拒绝，
    并且拒绝控制字符与所有可能闭合 HTML/CSS 语法的字符。

    注意：`render_markdown` 在交给解析器**之前**已经把裸 HTML 转义了，
    所以这里拿到的可能是 `&lt;script&gt;` 这种实体文本。因此除了 `<`/`>`
    本身，还要拒绝 `&`，否则 `&amp;lt;` 会原样进入属性值。
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or _CONTROL_RE.search(url):
        return None
    if any(character in url for character in ("\"", "'", "<", ">", "&", "`", "\\")):
        return None
    if url.startswith("//"):
        # 协议相对：会去请求任意第三方（视频也能当追踪像素用）——拒绝，
        # 站内媒体请写 /imgs/x.mp4 这样的单斜杠路径。
        return None
    match = _SCHEME_RE.match(url)
    if match is None:
        return url                       # 相对路径 / 锚点：放行
    return url if match.group(1).lower() in VIDEO_SCHEMES else None


class VideoDirectivePreprocessor(Preprocessor):
    """把 `@video(url)` 换成占位符，并记下待插入的 `<video>` 标签。

    为什么用占位符 + 后处理，而不是直接让预处理器吐出 HTML：
    渲染结果还要过一遍白名单净化（见模块开头），如果这里直接产出标签，
    就会与"正文里的裸 HTML 一律转义"的策略混淆。走占位符的话：

        @video(url)  ->  占位符  ->  净化器（纯文本原样保留）
                     ->  后处理替换成已校验的 <video>

    于是 URL 只经过 `safe_media_url` 这**一个**校验点，
    与链接/图片共享同一套白名单语义。
    """

    def __init__(self, md):
        super().__init__(md)
        self.replacements = []

    def run(self, lines):
        def substitute(match):
            url = safe_media_url(match.group("url"))
            if url is None:
                # 非法地址：保留原文可见，让作者一眼看出写错了
                return match.group(0)
            index = len(self.replacements)
            self.replacements.append(url)
            return f"{_SENTINEL_OPEN}{index}{_SENTINEL_CLOSE}"

        # 逐行处理并跳过代码区域（围栏与缩进代码块）：指令语法是行级的，
        # 代码里必须是原文。（与 `escape_raw_html_outside_code` 共用同一套判定，
        # 避免两处规则不一致。）
        flags = _code_block_flags(lines)
        return [line if in_code else VIDEO_RE.sub(substitute, line)
                for line, in_code in zip(lines, flags)]


class VideoDirectivePostprocessor(Postprocessor):
    """把占位符换成 `<video controls>` 标签（默认带原生控制条）。"""

    def __init__(self, md, replacements):
        super().__init__(md)
        self.replacements = replacements

    def run(self, text):
        if not self.replacements:
            return text
        pattern = re.compile(
            re.escape(_SENTINEL_OPEN) + r"(\d+)" + re.escape(_SENTINEL_CLOSE))

        def substitute(match):
            index = int(match.group(1))
            if index >= len(self.replacements):
                return match.group(0)
            url = self.replacements[index]
            safe = html_escape(url, quote=True)
            # 默认 controls；额外给 preload="metadata" 以免整段视频被预下载
            return (f'<video src="{safe}" controls preload="metadata">'
                    f'</video>')

        return pattern.sub(substitute, text)


class VideoDirectiveExtension(Extension):
    """注册 `@video(url)` 支持。"""

    def extendMarkdown(self, md):
        preprocessor = VideoDirectivePreprocessor(md)
        md.preprocessors.register(preprocessor, "elenvind_video", 35)
        md.postprocessors.register(
            VideoDirectivePostprocessor(md, preprocessor.replacements),
            "elenvind_video", 5)



def _safe_url(value: str):
    """URL 白名单校验：允许则返回原值，否则返回 None（调用方丢弃该属性）。"""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or _CONTROL_RE.search(url):
        return None
    if url.startswith("//"):
        # 协议相对 URL（//evil.test/x）会继承页面协议去请求**任意第三方**：
        # 内容作者不写 http(s):// 也能塞进外链图片/追踪像素，等于绕过白名单。
        # 站内资源请写单斜杠绝对路径（/imgs/x.png）。
        return None
    match = _SCHEME_RE.match(url)
    if match is None:
        return url                       # 相对路径 / 锚点 / 无 scheme：放行
    return url if match.group(1).lower() in ALLOWED_URL_SCHEMES else None


def _is_indented_code(line: str) -> bool:
    """该行是否形如缩进代码（4 空格或 1 个 Tab 起始）。"""
    return line.startswith("    ") or line.startswith("\t")


def _code_block_flags(lines):
    """标记哪些行属于**代码区域**（围栏代码块或缩进代码块）。

    为什么要自己判断：`render_markdown` 在交给解析器之前要先转义裸 HTML，
    而"代码块内的内容只能被转义一次"——交给 python-markdown 去转义。
    如果这里也转义一遍，`<script>` 就会变成 `&amp;lt;script&amp;gt;`，
    浏览器显示成 `&lt;script&gt;` 而不是 `<script>`。

    围栏（``` / ~~~）是显式的；缩进代码需要按 CommonMark 的规则近似判断：
    空行之后的"缩进 4 空格或 Tab"行开启代码块，遇到非空且未缩进的行结束。
    这是近似实现（不处理列表内的缩进代码等边界），但足以避免二次转义，
    并且"该转义时没转义"只会让预览更好看，不会造成安全问题——
    真正的安全由后面的白名单净化器兜底。
    """
    flags = []
    in_fence = False
    fence_marker = ""
    in_indented = False
    previous_blank = True
    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            flags.append(True)
            previous_blank = False
            continue
        if in_fence:
            flags.append(True)
            previous_blank = False
            continue
        if not line.strip():
            # 空行本身不算代码，但它开启"缩进代码块"的判定窗口
            flags.append(in_indented)
            previous_blank = True
            continue
        if in_indented:
            if _is_indented_code(line):
                flags.append(True)
                previous_blank = False
                continue
            in_indented = False
        if previous_blank and _is_indented_code(line):
            in_indented = True
            flags.append(True)
        else:
            flags.append(False)
        previous_blank = False
    return flags


def escape_raw_html_outside_code(source: str) -> str:
    """把**代码区域之外**的裸 HTML 开标签转义成文本。

    - 只处理标签的 `<`，因此 Markdown 语法（#、*、| 等）不受影响；
    - 代码区域（围栏与缩进代码块）内部保持原样，交给 python-markdown
      自己转义，避免出现 `&lt;` 被二次转义成 `&amp;lt;` 的显示缺陷。
    """
    lines = source.split("\n")
    flags = _code_block_flags(lines)
    return "\n".join(
        line if in_code else re.sub(r"<(?=[a-zA-Z/!?])", "&lt;", line)
        for line, in_code in zip(lines, flags)
    )


class _Sanitizer(HTMLParser):
    """基于标准库的白名单净化器。

    - 白名单内的标签/属性：重建输出（属性逐个校验）；
    - 白名单外的标签：转义成可见文本（不静默丢内容）；
    - 危险容器标签（script/style/iframe/...）：连同内容整体删除；
    - 危险协议：丢弃该属性（标签保留）；
    - 注释 / DOCTYPE / 处理指令：丢弃。
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self._drop_depth = 0

    # ----- 标签 -----
    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag in ALLOWED_TAGS:
            self.parts.append(self._render_tag(tag, attrs, closed=False))
        else:
            self.parts.append(html_escape(self.get_starttag_text() or f"<{tag}>",
                                          quote=False))

    def handle_startendtag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            return
        if self._drop_depth:
            return
        if tag in ALLOWED_TAGS:
            self.parts.append(self._render_tag(tag, attrs, closed=True))
        else:
            self.parts.append(html_escape(self.get_starttag_text() or f"<{tag}/>",
                                          quote=False))

    def handle_endtag(self, tag):
        if tag in DROP_WITH_CONTENT:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag in ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")
        elif tag not in ALLOWED_TAGS:
            self.parts.append(html_escape(f"</{tag}>", quote=False))

    # ----- 文本与实体 -----
    def handle_data(self, data):
        if self._drop_depth:
            return
        self.parts.append(html_escape(data, quote=False))

    def handle_entityref(self, name):
        if self._drop_depth:
            return
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._drop_depth:
            return
        self.parts.append(f"&#{name};")

    # ----- 其它一律丢弃 -----
    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    def _render_tag(self, tag, attrs, closed):
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, frozenset())
        rendered = []
        href = None
        for name, value in attrs:
            name = (name or "").lower()
            if name not in allowed:
                continue
            if value is None:
                rendered.append(name)
                continue
            if name in _URL_ATTRS:
                safe = _safe_url(value)
                if safe is None:
                    continue
                if name == "href":
                    href = safe
                rendered.append(f'{name}="{html_escape(safe, quote=True)}"')
            else:
                if _CONTROL_RE.search(value):
                    continue
                rendered.append(f'{name}="{html_escape(value, quote=True)}"')
        if tag == "a" and href and _SCHEME_RE.match(href) and not any(
                item.startswith("rel=") for item in rendered):
            rendered.append('rel="noopener noreferrer"')
        attr_text = (" " + " ".join(rendered)) if rendered else ""
        return f"<{tag}{attr_text}>"


def sanitize_html(html: str) -> str:
    """对 HTML 片段做白名单净化（Core 内部使用，也可用于其它来源的片段）。"""
    parser = _Sanitizer()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


_converter = None


def _build_converter() -> Markdown:
    """新建一个转换器实例（含视频指令扩展）。"""
    return Markdown(
        extensions=_EXTENSIONS + [VideoDirectiveExtension()],
        extension_configs=_EXTENSION_CONFIGS,
        output_format="html",
        tab_length=4,
    )


def _get_converter() -> Markdown:
    """无状态场景共用的转换器单例（**不含**视频指令扩展）。

    视频扩展的预处理器会在实例上累积 `replacements`，共用单例会跨请求串数据，
    因此含 `@video(...)` 的正文走一次性实例（见 `render_markdown`）。
    """
    global _converter
    if _converter is None:
        _converter = Markdown(
            extensions=_EXTENSIONS,
            extension_configs=_EXTENSION_CONFIGS,
            output_format="html",
            tab_length=4,
        )
    return _converter


def render_markdown(source: str) -> Markup:
    """把 Markdown 正文渲染为**可安全嵌入页面**的 HTML。

    返回 `Markup`：模板里 `{{ article.body }}` 直接输出即可，无需 `|safe`。
    安全保证：裸 HTML 先转义、渲染结果再过白名单净化，
    危险协议不会进入 href/src，script/iframe/style 连内容一起丢弃。

    支持的额外指令：`@video(url)` -> `<video src="url" controls>`。
    """
    if source is None:
        return Markup("")
    if not isinstance(source, str):
        raise TypeError(f"markdown source must be str, got {type(source).__name__}")
    if not source.strip():
        return Markup("")

    # 只在真的用了指令时才付出"新建实例"的代价；绝大多数正文走单例。
    if VIDEO_MARKER in source:
        converter = _build_converter()
        rendered = converter.convert(escape_raw_html_outside_code(source))
        return Markup(sanitize_html(rendered))

    converter = _get_converter()
    converter.reset()
    rendered = converter.convert(escape_raw_html_outside_code(source))
    return Markup(sanitize_html(rendered))


