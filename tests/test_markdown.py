"""Core Markdown 契约测试：唯一入口 + 白名单净化。

判定方式是 **DOM 解析**而不是字符串匹配：把渲染结果交给标准库 HTMLParser，
检查是否真的出现了危险标签 / 事件属性 / 危险协议。
字符串包含判断会把安全输出（如 `&lt;script&gt;`）误判为漏洞。
"""
import unittest
from html.parser import HTMLParser

from tests.support import PROJECT_ROOT  # 导入即完成 sys.path 设置

from elenvind.core.markdown import (
    ALLOWED_TAGS,
    escape_raw_html_outside_code,
    render_markdown,
    render_markdown_inline,
    sanitize_html,
)

DANGEROUS_TAGS = frozenset({"script", "iframe", "object", "embed", "style", "svg",
                            "math", "form", "input", "button", "template", "noscript"})
URL_ATTRS = frozenset({"href", "src", "poster", "action", "cite"})
SAFE_URL_PREFIXES = ("http://", "https://", "mailto:", "/", "#", "?")


class _Audit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.events = []
        self.bad_urls = []
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            name = (name or "").lower()
            self.attrs.append((tag, name, value))
            if name.startswith("on"):
                self.events.append((tag, name, value))
            if name in URL_ATTRS and value:
                lowered = value.strip().lower()
                if not lowered.startswith(SAFE_URL_PREFIXES):
                    self.bad_urls.append((tag, name, value))

    handle_startendtag = handle_starttag


def audit(html: str) -> _Audit:
    parser = _Audit()
    parser.feed(str(html))
    parser.close()
    return parser


class MarkdownSafetyTests(unittest.TestCase):
    def assert_safe(self, source):
        result = audit(render_markdown(source))
        self.assertEqual(set(result.tags) & DANGEROUS_TAGS, set(),
                         f"dangerous tags from {source!r}: {result.tags}")
        self.assertEqual(result.events, [], f"event attributes from {source!r}")
        self.assertEqual(result.bad_urls, [], f"dangerous urls from {source!r}")
        return str(render_markdown(source))

    def test_raw_script_is_rendered_as_visible_text(self):
        out = self.assert_safe("<script>alert(1)</script>")
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("alert(1)", out)      # 内容保留为可见文本，不静默丢弃

    def test_raw_html_event_handlers_are_neutralised(self):
        self.assert_safe('text <img src=x onerror="alert(1)"> end')
        self.assert_safe('<p onclick="alert(1)">x</p>')
        self.assert_safe("<svg onload=alert(1)></svg>")

    def test_dangerous_containers_are_removed_with_content(self):
        for payload in ("<iframe src=https://evil.test></iframe>",
                        "<style>body{display:none}</style>",
                        "<object data=x></object>",
                        "<template><script>x</script></template>"):
            with self.subTest(payload=payload):
                self.assert_safe(payload)

    def test_markdown_links_with_dangerous_schemes_lose_href(self):
        for scheme in ("javascript:alert(1)", "vbscript:msgbox(1)",
                       "data:text/html,<script>alert(1)</script>",
                       "file:///etc/passwd", "JaVaScRiPt:alert(1)"):
            with self.subTest(scheme=scheme):
                out = self.assert_safe(f"[x]({scheme})")
                self.assertIn("x", out)
                self.assertNotIn("href", out.lower())

    def test_markdown_images_with_dangerous_schemes_lose_src(self):
        for scheme in ("javascript:alert(1)", "data:image/svg+xml,<svg onload=alert(1)>"):
            with self.subTest(scheme=scheme):
                out = self.assert_safe(f"![alt]({scheme})")
                self.assertNotIn("src=", out)

    def test_crlf_in_url_is_rejected(self):
        out = self.assert_safe("[x](https://e.com/a\nX-Injected: 1)")
        self.assertNotIn("X-Injected", out)

    def test_comments_are_dropped(self):
        out = self.assert_safe("<!-- <script>alert(1)</script> -->")
        self.assertNotIn("<!--", out)

    def test_safe_urls_survive_and_get_noopener(self):
        out = self.assert_safe("[ok](https://e.com/path?a=1&b=2)")
        self.assertIn('href="https://e.com/path?a=1&amp;b=2"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_relative_and_anchor_urls_are_allowed(self):
        self.assertIn('href="/about"', self.assert_safe("[a](/about)"))
        self.assertIn('href="#fn:1"', self.assert_safe("[a](#fn:1)"))

    def test_attributes_are_whitelisted(self):
        """attr_list 允许 width；危险属性（onmouseover 等）不保留。"""
        out = self.assert_safe("![i](https://e.com/a.png){: width=200 }")
        self.assertIn('width="200"', out)
        out = self.assert_safe('![i](https://e.com/a.png){: onmouseover=alert(1) }')
        self.assertNotIn("onmouseover", out)

    def test_attribute_value_escaping(self):
        out = self.assert_safe('![i](https://e.com/a.png){: title="\\" onload=\\"x" }')
        self.assertNotIn('onload="x"', out)


class MarkdownFeatureTests(unittest.TestCase):
    def test_commonmark_subset(self):
        self.assertIn("<strong>bold</strong>", str(render_markdown("**bold**")))
        self.assertIn("<em>it</em>", str(render_markdown("*it*")))
        self.assertIn("<h1", str(render_markdown("# Head")))
        self.assertIn("<blockquote>", str(render_markdown("> quote")))
        self.assertIn("<ul>", str(render_markdown("- one\n- two")))
        self.assertIn("<ol>", str(render_markdown("1. one\n2. two")))

    def test_tables_and_footnotes_and_code(self):
        table = str(render_markdown("| a | b |\n|---|---|\n| 1 | 2 |"))
        self.assertIn("<table>", table)
        self.assertIn("<td>1</td>", table)
        footnote = str(render_markdown("note[^1]\n\n[^1]: text"))
        self.assertIn("footnote", footnote)
        code = str(render_markdown("```py\nprint('<x>')\n```"))
        self.assertIn('class="language-py"', code)
        self.assertIn("&lt;x&gt;", code)          # 单次转义
        self.assertNotIn("&amp;lt;", code)        # 不得二次转义

    def test_code_fence_content_is_not_pre_escaped(self):
        source = "```\n<script>alert(1)</script>\n```"
        out = str(render_markdown(source))
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("&amp;lt;", out)
        audit(out)  # 结构审计：不应出现真实 script 标签

    def test_unicode_chinese_emoji(self):
        out = str(render_markdown("# 标题 🎉\n\n中文段落 émoji"))
        self.assertIn("标题 🎉", out)
        self.assertIn("中文段落 émoji", out)

    def test_empty_and_none_input(self):
        self.assertEqual(str(render_markdown("")), "")
        self.assertEqual(str(render_markdown("   ")), "")
        self.assertEqual(str(render_markdown(None)), "")

    def test_non_string_raises(self):
        with self.assertRaises(TypeError):
            render_markdown(123)

    def test_returns_markup_so_templates_need_no_safe_filter(self):
        from markupsafe import Markup
        self.assertIsInstance(render_markdown("**x**"), Markup)

    def test_inline_variant_escapes_and_sanitises(self):
        out = str(render_markdown_inline("**bold**"))
        self.assertIn("<strong>bold</strong>", out)
        # 行内变体同样必须净化
        self.assertNotIn("<script", str(render_markdown_inline("<script>alert(1)</script>")))

    def test_deterministic_output(self):
        source = "# H\n\ntext **b** `c`\n\n| a |\n|---|\n| 1 |\n"
        outputs = {str(render_markdown(source)) for _ in range(5)}
        self.assertEqual(len(outputs), 1)


class RawHtmlPreEscapeTests(unittest.TestCase):
    def test_tags_outside_fences_are_escaped(self):
        out = escape_raw_html_outside_code("<b>x</b> and <script>y</script>")
        self.assertIn("&lt;b>x&lt;/b>", out)
        self.assertIn("&lt;script>", out)

    def test_tags_inside_fences_are_untouched(self):
        source = "```\n<script>\n```\n<p>after</p>"
        out = escape_raw_html_outside_code(source)
        self.assertIn("<script>", out)          # 围栏内保持原样
        self.assertIn("&lt;p>after&lt;/p>", out)


class VideoDirectiveTests(unittest.TestCase):
    """`@video(url)` 指令：默认带 controls，地址走白名单，围栏内不生效。"""

    def render(self, source):
        return str(render_markdown(source))

    def test_basic_render_has_controls(self):
        out = self.render("@video(https://example.com/clip.mp4)")
        self.assertIn('<video src="https://example.com/clip.mp4"', out)
        self.assertIn("controls", out)
        self.assertIn("</video>", out)

    def test_site_relative_url_is_allowed(self):
        out = self.render("@video(/imgs/clip.mp4)")
        self.assertIn('src="/imgs/clip.mp4"', out)

    def test_leading_indent_works(self):
        out = self.render("   @video(https://e.com/a.mp4)")
        self.assertIn("<video", out)

    def test_surrounded_by_paragraphs(self):
        out = self.render("before\n\n@video(https://e.com/a.mp4)\n\nafter")
        self.assertIn("<p>before</p>", out)
        self.assertIn("<video", out)
        self.assertIn("<p>after</p>", out)

    def test_multiple_directives_keep_their_own_urls(self):
        out = self.render("@video(https://e.com/a.mp4)\n\n@video(https://e.com/b.mp4)")
        self.assertEqual(out.count("<video"), 2)
        self.assertIn("https://e.com/a.mp4", out)
        self.assertIn("https://e.com/b.mp4", out)

    def test_dangerous_urls_are_rejected_and_left_visible(self):
        for hostile in (
            "@video(javascript:alert(1))",
            "@video(vbscript:msgbox(1))",
            "@video(data:video/mp4;base64,AAAA)",
            "@video(file:///etc/passwd)",
            '@video(https://e.com/a.mp4" onload="alert(1))',
            "@video(https://e.com/<script>)",
            "@video(https://e.com/a.mp4&amp;x=1)",
            "@video(https://e.com/a`b)",
        ):
            with self.subTest(source=hostile):
                out = self.render(hostile)
                self.assertNotIn("<video", out, out)
                # 原文保留可见，作者能看出写错了
                self.assertIn("@video", out)
                # 关键：用 DOM 审计确认没有任何**真实**属性/标签被注入。
                # 字符串里出现 "onload" 只是可见文本，不是属性。
                result = audit(out)
                self.assertEqual(set(result.tags) & {"video", "source", "script"},
                                 set(), out)
                self.assertEqual(result.events, [], out)
                self.assertEqual(result.bad_urls, [], out)

    def test_not_interpreted_inside_fenced_code(self):
        out = self.render("```\n@video(https://e.com/a.mp4)\n```")
        self.assertNotIn("<video", out)
        self.assertIn("@video(https://e.com/a.mp4)", out)

    def test_not_interpreted_inside_inline_code(self):
        out = self.render("`@video(https://e.com/a.mp4)`")
        self.assertNotIn("<video", out)
        self.assertIn("<code>@video(https://e.com/a.mp4)</code>", out)

    def test_directive_must_be_on_its_own_line(self):
        """行中间的指令不生效（避免误伤散文里的 @video(...) 写法）。"""
        out = self.render("see @video(https://e.com/a.mp4) for details")
        self.assertNotIn("<video", out)

    def test_renders_are_independent(self):
        """回归：扩展在实例上累积状态，共用单例会跨请求串 URL。"""
        for index in range(5):
            out = self.render(f"@video(https://e.com/v{index}.mp4)")
            self.assertIn(f"https://e.com/v{index}.mp4", out)
            self.assertEqual(out.count("<video"), 1, out)

    def test_same_input_is_idempotent(self):
        source = "@video(https://e.com/same.mp4)"
        outputs = {self.render(source) for _ in range(4)}
        self.assertEqual(len(outputs), 1)

    def test_output_passes_dom_audit(self):
        """指令产出的 video 必须能过结构审计（真实标签、无事件属性）。"""
        out = self.render("@video(https://e.com/a.mp4)")
        result = audit(out)
        self.assertIn("video", result.tags)
        self.assertEqual(result.events, [])
        self.assertEqual(result.bad_urls, [])

    def test_malformed_directives_do_not_crash(self):
        for source in ("@video(", "@video()", "@video(   )", "@video(a b)",
                       "@video(https://e.com/a.mp4", "@video)"):
            with self.subTest(source=source):
                out = self.render(source)
                self.assertNotIn("<video", out, out)


class CodeRegionTests(unittest.TestCase):
    """代码区域（围栏 + 缩进代码块）内只允许转义一次。

    回归：`escape_raw_html_outside_code` 早期只看围栏，缩进代码块里的
    `<script>` 会被转义成 `&lt;`，随后 python-markdown 再转义一次变成
    `&amp;lt;`，浏览器显示成 `&lt;script&gt;`（多了一层）。
    """

    SOURCES = (
        "```\n<script>x</script>\n```",
        "```html\n<script>x</script>\n```",
        "~~~\n<script>x</script>\n~~~",
        "    <script>x</script>",
        "前文\n\n    <b>a</b>\n    <i>b</i>\n\n后文",
    )

    def test_code_regions_escape_exactly_once(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                out = str(render_markdown(source))
                self.assertNotIn("&amp;lt;", out, out)
                self.assertNotIn("&amp;gt;", out, out)
                self.assertIn("&lt;script&gt;" if "script" in source else "&lt;b&gt;",
                              out, out)

    def test_code_regions_never_contain_real_tags(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                out = str(render_markdown(source))
                result = audit(out)
                self.assertEqual(set(result.tags) & {"script", "b", "i"}, set(), out)

    def test_body_html_escaped_once(self):
        out = str(render_markdown("文字 <script>x</script> 文字"))
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("&amp;lt;", out)

    def test_indented_paragraph_continuation_is_not_code(self):
        """段落内的普通缩进不该被当成代码块（会变成 <pre> 就错了）。"""
        out = str(render_markdown("文字\n    缩进的继续段落"))
        self.assertNotIn("<pre>", out)
        self.assertIn("<p>", out)

    def test_directive_ignored_in_both_code_kinds(self):
        for source in ("```\n@video(https://e.com/a.mp4)\n```",
                       "    @video(https://e.com/a.mp4)",
                       "~~~\n@video(https://e.com/a.mp4)\n~~~"):
            with self.subTest(source=source):
                out = str(render_markdown(source))
                self.assertNotIn("<video", out, out)
                self.assertIn("@video(https://e.com/a.mp4)", out)

    def test_escape_helper_agrees_with_renderer(self):
        """两个入口共用同一套代码区域判定，避免规则漂移。"""
        source = "前文\n\n    <b>a</b>\n\n后文\n\n<script>x</script>"
        escaped = escape_raw_html_outside_code(source)
        self.assertIn("<b>a</b>", escaped)          # 代码区域内原样保留
        self.assertIn("&lt;script>", escaped)        # 代码区域外已转义


class SanitizerUnitTests(unittest.TestCase):
    def test_unknown_tag_becomes_visible_text(self):
        self.assertEqual(sanitize_html("<marquee>hi</marquee>"),
                         "&lt;marquee&gt;hi&lt;/marquee&gt;")

    def test_allowed_tag_and_attr_survive(self):
        self.assertEqual(sanitize_html('<a href="https://e.com">x</a>'),
                         '<a href="https://e.com" rel="noopener noreferrer">x</a>')

    def test_entities_are_preserved(self):
        self.assertEqual(sanitize_html("<p>a &amp; b &#39;c&#39;</p>"),
                         "<p>a &amp; b &#39;c&#39;</p>")

    def test_dangerous_container_content_is_dropped(self):
        self.assertEqual(sanitize_html("<script>alert(1)</script>ok"), "ok")

    def test_allowed_tags_are_a_closed_set(self):
        """白名单不得包含任何危险标签（防止有人往白名单里加 script）。"""
        self.assertEqual(ALLOWED_TAGS & DANGEROUS_TAGS, frozenset())

    def test_no_third_party_html_sanitizer_imported(self):
        """净化器必须只用标准库（不引入 bleach 等）。"""
        source = (PROJECT_ROOT / "elenvind" / "core" / "markdown.py").read_text(
            encoding="utf-8")
        for banned in ("import bleach", "from bleach", "import lxml", "import nh3"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
