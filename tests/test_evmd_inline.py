"""EVMD 行内语法测试：转义、code、粗斜体、删除线、链接、脚注引用、注入防护。"""
import unittest

from tests.support import PROJECT_ROOT  # noqa: F401

from elenvind.evmd_inline import (
    MAX_INLINE_LENGTH,
    escape_html,
    parse_inline,
    safe_url,
)


class SafeUrlTests(unittest.TestCase):
    def test_allowed_schemes(self):
        for url in ("http://example.com/a?b=1&c=2", "https://example.com",
                    "HTTPS://EXAMPLE.COM/x", "http://example.com:8080/p#f"):
            with self.subTest(url=url):
                self.assertNotEqual(safe_url(url), "")

    def test_dangerous_schemes_rejected(self):
        for url in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<script>",
                    "vbscript:msgbox(1)", "file:///etc/passwd", "ftp://example.com/x",
                    "//evil.example.com/x", "/relative/path", "mailto:a@b.c",
                    "tel:+123", "about:blank", "blob:http://x/y"):
            with self.subTest(url=url):
                self.assertEqual(safe_url(url), "")

    def test_surrounding_whitespace_is_trimmed_but_inner_is_rejected(self):
        self.assertEqual(safe_url("  http://a/b  "), "http://a/b")
        self.assertEqual(safe_url("http://a/b\r\nX-Injected: 1"), "")
        self.assertEqual(safe_url("http://a/b\tc"), "")
        self.assertEqual(safe_url("http://a/b\x00c"), "")

    def test_empty_host_rejected(self):
        for url in ("http://", "https://", "http:///path"):
            with self.subTest(url=url):
                self.assertEqual(safe_url(url), "")

    def test_non_string_input(self):
        self.assertEqual(safe_url(None), "")
        self.assertEqual(safe_url(123), "")


class EscapeTests(unittest.TestCase):
    def test_escape_html_escapes_quotes(self):
        self.assertEqual(escape_html('<a href="x">&\''),
                         "&lt;a href=&quot;x&quot;&gt;&amp;&#x27;")


class InlineSyntaxTests(unittest.TestCase):
    def test_plain_text_is_escaped(self):
        self.assertEqual(parse_inline("<b>bold</b>"), "&lt;b&gt;bold&lt;/b&gt;")
        self.assertEqual(parse_inline('a "quoted" b'), "a &quot;quoted&quot; b")

    def test_code_span(self):
        self.assertEqual(parse_inline("`a < b & c`"),
                         "<code>a &lt; b &amp; c</code>")
        self.assertEqual(parse_inline("`**not bold**`"), "<code>**not bold**</code>")
        self.assertEqual(parse_inline("``"), "``")   # 空代码跨度不匹配

    def test_bold_and_italic(self):
        self.assertEqual(parse_inline("**bold**"), "<strong>bold</strong>")
        self.assertEqual(parse_inline("*italic*"), "<em>italic</em>")
        self.assertEqual(parse_inline("_italic_"), "<em>italic</em>")

    def test_underscore_italic_does_not_break_words_or_urls(self):
        self.assertEqual(parse_inline("snake_case_name"), "snake_case_name")
        self.assertEqual(parse_inline("http://a/b_c_d"), "http://a/b_c_d")
        self.assertEqual(parse_inline("中文_强调_中文"), "中文<em>强调</em>中文")

    def test_strikethrough(self):
        self.assertEqual(parse_inline("~~gone~~"), "<del>gone</del>")

    def test_nested_emphasis_is_not_supported(self):
        """EVMD 有意不实现 *** 嵌套强调（见 EVMD_SPEC 支持对照表）。

        行为锁定：不会崩、不会产生非法 HTML，但也不渲染成嵌套 strong/em。
        """
        output = parse_inline("**bold *inner* text**")
        self.assertNotIn("<strong>", output)
        self.assertIsInstance(output, str)

    def test_escaped_format_characters(self):
        self.assertEqual(parse_inline(r"\*not italic\*"), "*not italic*")
        self.assertEqual(parse_inline(r"\_not italic\_"), "_not italic_")
        self.assertEqual(parse_inline(r"\~\~no del\~\~"), "~~no del~~")

    def test_backtick_is_always_a_code_delimiter(self):
        """EVMD 的代码跨度优先于反斜杠转义：\\` 不能"关掉"代码跨度。

        这是与 CommonMark 的有意差异（代码跨度内容按原样处理），
        行为已写入 docs/EVMD_SPEC.md，此处锁定回归。
        """
        self.assertEqual(parse_inline(r"\`not code\`"), r"\<code>not code\</code>")

    def test_escaped_html_metacharacters_cannot_inject(self):
        """\\< 等转义序列必须输出转义后的字面量（历史 XSS 修复点）。"""
        self.assertEqual(parse_inline(r"\<script\>alert(1)\</script\>"),
                         "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertEqual(parse_inline(r"a \& b"), "a &amp; b")
        self.assertEqual(parse_inline(r"\<img src=x onerror=alert(1)\>"),
                         "&lt;img src=x onerror=alert(1)&gt;")

    def test_raw_html_is_never_executable(self):
        payloads = (
            "<script>alert(1)</script>",
            '<img src=x onerror="alert(1)">',
            '<svg/onload=alert(1)>',
            "<iframe src=javascript:alert(1)>",
            '<a href="javascript:alert(1)">x</a>',
            "<style>body{display:none}</style>",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                output = parse_inline(payload)
                self.assertNotIn("<script", output)
                self.assertNotIn("<img", output)
                self.assertNotIn("<svg", output)
                self.assertNotIn("<iframe", output)
                self.assertNotIn("<style", output)


class InlineLinkTests(unittest.TestCase):
    def test_link_renders_anchor(self):
        self.assertEqual(parse_inline("@{link,https://example.com,Example}"),
                         '<a href="https://example.com">Example</a>')

    def test_query_ampersand_is_not_double_escaped(self):
        self.assertEqual(parse_inline("@{link,https://example.com/a?b=1&c=2,text}"),
                         '<a href="https://example.com/a?b=1&amp;c=2">text</a>')

    def test_dangerous_scheme_degrades_to_text(self):
        output = parse_inline("@{link,javascript:alert(1),click}")
        self.assertNotIn("<a", output)
        self.assertNotIn("javascript:", output)
        self.assertEqual(output, "click")

    def test_quote_in_url_cannot_escape_attribute(self):
        output = parse_inline('@{link,http://x/"onmouseover="alert(1),x}')
        # 引号必须被转义成实体，而不是原样进入属性
        self.assertNotIn('"onmouseover="alert(1)"', output)
        self.assertNotIn('href="http://x/"onmouseover', output)
        self.assertIn("&quot;onmouseover=&quot;", output)
        self.assertTrue(output.startswith('<a href="http://x/'))

    def test_link_text_is_escaped(self):
        output = parse_inline("@{link,https://example.com,<b>bold</b>}")
        self.assertIn("<a href=\"https://example.com\">", output)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", output)

    def test_malformed_links_are_left_as_text(self):
        for raw in ("@{link}", "@{link,}", "@{link,,}", "@{link,http://a}",
                    "@{link http://a,text}", "@{link,http://a,text"):
            with self.subTest(raw=raw):
                output = parse_inline(raw)
                self.assertNotIn("<a ", output)
                self.assertNotIn("<a>", output)
                self.assertIn("&lt;" if "<" in raw else "@{link", output)


class InlineFootnoteTests(unittest.TestCase):
    def test_ref_becomes_placeholder_token(self):
        output = parse_inline("see @{ref,note1} here")
        # 块级引擎负责编号，此处只应留下私有区占位符
        self.assertIn("\ue300note1\ue301", output)
        self.assertNotIn("@{ref", output)

    def test_malformed_ref_is_left_alone(self):
        for raw in ("@{ref}", "@{ref,}", "@{ref,has space}", "@{ref," + "x" * 40 + "}"):
            with self.subTest(raw=raw):
                self.assertIn("@{ref", parse_inline(raw))


class InlineRobustnessTests(unittest.TestCase):
    def test_empty_and_whitespace_documents(self):
        self.assertEqual(parse_inline(""), "")
        self.assertEqual(parse_inline("   "), "   ")
        self.assertEqual(parse_inline("\n"), "\n")

    def test_unicode_and_emoji_survive(self):
        text = "中文测试 🎉 émoji Ünïcode 🚀"
        self.assertEqual(parse_inline(text), text)

    def test_very_long_paragraph_is_plain_escaped(self):
        raw = "`x` " * (MAX_INLINE_LENGTH // 3)
        output = parse_inline(raw)
        self.assertNotIn("<code>", output)
        self.assertLessEqual(len(output), len(raw) * 6)

    def test_unbalanced_markers_do_not_raise_or_hang(self):
        for raw in ("*", "**", "`", "~~", "_", "@{link,", "***a", "a***",
                    "**a*", "*a**", "`a", "a`", "~~a", "a~~"):
            with self.subTest(raw=raw):
                self.assertIsInstance(parse_inline(raw), str)

    def test_placeholder_lookalikes_are_not_special(self):
        """用户输入若恰好包含私有区字符，不应被误认为内部占位符。

        例外：\\ue001 是块级引擎的硬换行占位符，行内层必须原样保留
        （由 _flush_paragraph 替换为 <br>）；块级入口会先清掉所有其它私有区字符。
        """
        for raw in ("text \ue1000\ue101 end",
                    "\ue1009\ue101",           # 越界序号：曾经会 IndexError → 500
                    "\ue4000\ue401",
                    "a\ue1000\ue101b"):
            with self.subTest(raw=raw):
                output = parse_inline(raw)
                self.assertIsInstance(output, str)
                self.assertNotIn("<code>", output)
                self.assertNotIn("\ue100", output)

    def test_repeated_parsing_is_stable(self):
        """输出再解析一次不应产生新的标签（防双重解释）。"""
        once = parse_inline("**bold** and @{link,https://a.b,c} and `code`")
        twice = parse_inline(once)
        self.assertNotIn("<strong>", twice)
        self.assertNotIn("<code>", twice)


if __name__ == "__main__":
    unittest.main()
