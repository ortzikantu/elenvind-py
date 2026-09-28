"""EVMD 块级解析测试：文档头、优先级顺序、代码块、列表、引用、表格、脚注、资源上限。"""
import unittest

from tests.support import PROJECT_ROOT  # noqa: F401

from elenvind.evmd_header import EvmdError, normalize_line_endings, split_document
from elenvind.evmd_parser import (
    MAX_DOCUMENT_LINES,
    MAX_FOOTNOTES,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    document_meta,
    evmd_to_html,
    parse_document,
)


class DocumentHeaderTests(unittest.TestCase):
    def test_splits_header_and_body(self):
        text = '@@@\ntitle = "Hi"\ndate = 2026-01-01\n@@@\n\nBody text\n'
        meta, body = split_document(text)
        self.assertEqual(meta["title"], "Hi")
        self.assertIn("Body text", body)

    def test_document_without_header_is_returned_verbatim(self):
        meta, body = split_document("just body\n")
        self.assertIsNone(meta)
        self.assertEqual(body, "just body\n")

    def test_unclosed_header_raises(self):
        with self.assertRaises(EvmdError):
            split_document("@@@\ntitle = \"x\"\n")

    def test_invalid_toml_raises_evmd_error(self):
        with self.assertRaises(EvmdError):
            split_document("@@@\ntitle = \n@@@\n")

    def test_crlf_line_endings_are_supported(self):
        """Windows 写出的 CRLF 文件必须能正确识别文档头（历史缺陷）。"""
        text = '@@@\r\ntitle = "CRLF"\r\n@@@\r\n\r\nBody\r\n'
        meta, body = split_document(text)
        self.assertEqual(meta["title"], "CRLF")
        self.assertIn("Body", body)

    def test_header_at_end_of_file_without_trailing_newline(self):
        meta, body = split_document('@@@\ntitle = "EOF"\n@@@')
        self.assertEqual(meta["title"], "EOF")
        self.assertEqual(body, "")

    def test_marker_with_trailing_spaces_is_not_a_header(self):
        meta, _ = split_document("@@@ \ntitle = 1\n@@@\n")
        self.assertIsNone(meta)

    def test_normalize_line_endings(self):
        self.assertEqual(normalize_line_endings("a\r\nb\rc\nd"), "a\nb\nc\nd")
        with self.assertRaises(EvmdError):
            normalize_line_endings(None)

    def test_document_meta_requires_header(self):
        self.assertEqual(document_meta("no header"), {})
        with self.assertRaises(EvmdError):
            document_meta("no header", require_header=True)

    def test_parse_document_returns_meta_and_html(self):
        meta, html = parse_document('@@@\ntitle = "T"\n@@@\n\n# Heading\n')
        self.assertEqual(meta["title"], "T")
        self.assertIn("<h1>Heading</h1>", html)

    def test_empty_document(self):
        self.assertEqual(evmd_to_html(""), "")
        self.assertEqual(evmd_to_html("\n\n\n"), "")

    def test_non_string_document_raises(self):
        with self.assertRaises(EvmdError):
            evmd_to_html(None)


class BlockSyntaxTests(unittest.TestCase):
    def test_atx_headings(self):
        for level in range(1, 7):
            with self.subTest(level=level):
                html = evmd_to_html("#" * level + " Title")
                self.assertIn(f"<h{level}>Title</h{level}>", html)
        # 7 个 # 不是标题
        self.assertNotIn("<h", evmd_to_html("####### Title"))

    def test_heading_requires_space(self):
        self.assertNotIn("<h1>", evmd_to_html("#NoSpace"))

    def test_setext_headings(self):
        self.assertIn("<h1>Title</h1>", evmd_to_html("Title\n==="))
        self.assertIn("<h2>Title</h2>", evmd_to_html("Title\n---"))

    def test_hr_requires_empty_paragraph(self):
        self.assertIn("<hr>", evmd_to_html("text\n\n---"))
        self.assertIn("<hr>", evmd_to_html("***"))
        self.assertIn("<hr>", evmd_to_html("___"))

    def test_paragraph_joining_and_hard_break(self):
        self.assertIn("<p>a b</p>", evmd_to_html("a\nb"))
        self.assertIn("<p>a<br>b</p>", evmd_to_html("a  \nb"))
        self.assertIn("<p>a<br>b</p>", evmd_to_html("a\\\nb"))

    def test_blank_line_splits_paragraphs(self):
        html = evmd_to_html("one\n\ntwo")
        self.assertIn("<p>one</p>", html)
        self.assertIn("<p>two</p>", html)

    def test_fenced_code_block(self):
        html = evmd_to_html("```py\nprint('<x>')\n```")
        self.assertIn('<pre><code class="language-py">', html)
        self.assertIn("print(&#x27;&lt;x&gt;&#x27;)", html)

    def test_fenced_code_language_whitelist(self):
        html = evmd_to_html('```"><script>alert(1)</script>\nx\n```')
        self.assertNotIn("<script", html)
        self.assertIn("<pre><code>", html)

    def test_unclosed_fence_still_closes(self):
        html = evmd_to_html("```\ncode line")
        self.assertIn("<pre><code>code line</code></pre>", html)

    def test_indented_code_block(self):
        html = evmd_to_html("    indented code")
        self.assertIn("<pre><code>indented code</code></pre>", html)

    def test_unordered_and_ordered_lists(self):
        self.assertIn("<ul><li>a</li><li>b</li></ul>", evmd_to_html("- a\n- b"))
        self.assertIn("<ul><li>a</li></ul>", evmd_to_html("* a"))
        self.assertIn("<ol><li>a</li><li>b</li></ol>", evmd_to_html("1. a\n2. b"))

    def test_blockquote(self):
        self.assertIn("<blockquote>quoted <strong>b</strong></blockquote>",
                      evmd_to_html("> quoted **b**"))
        self.assertIn("<blockquote>a b</blockquote>", evmd_to_html("> a\n> b"))

    def test_fence_beats_everything_inside(self):
        html = evmd_to_html("```\n# not a heading\n- not a list\n> not a quote\n```")
        self.assertNotIn("<h1>", html)
        self.assertNotIn("<ul>", html)
        self.assertNotIn("<blockquote>", html)

    def test_directive_lines_before_and_after_paragraphs(self):
        html = evmd_to_html("text\n\n@{video, https://e.com/v.mp4}\n\nmore")
        self.assertIn("<video", html)
        self.assertIn("<p>text</p>", html)
        self.assertIn("<p>more</p>", html)

    def test_unicode_chinese_and_emoji(self):
        html = evmd_to_html("# 标题 🎉\n\n中文段落 ✅ émoji")
        self.assertIn("<h1>标题 🎉</h1>", html)
        self.assertIn("中文段落 ✅ émoji", html)

    def test_very_long_line(self):
        long_line = "x" * 200_000
        html = evmd_to_html(long_line)
        self.assertIn(long_line, html)

    def test_document_line_limit(self):
        with self.assertRaises(EvmdError):
            evmd_to_html("a\n" * (MAX_DOCUMENT_LINES + 1))

    def test_private_use_characters_cannot_forge_placeholders(self):
        html = evmd_to_html("text \ue300forged\ue301 and \ue1000\ue101")
        self.assertNotIn("footnote-ref", html)
        self.assertNotIn("<code>", html)


class TableTests(unittest.TestCase):
    def test_basic_table(self):
        html = evmd_to_html("@{table}\n| A | B |\n| 1 | 2 |")
        self.assertIn("<table><thead><tr><th>A</th><th>B</th></tr></thead>", html)
        self.assertIn("<tbody><tr><td>1</td><td>2</td></tr></tbody>", html)

    def test_alignment_row(self):
        html = evmd_to_html("@{table}\n| A | B | C |\n| :--- | ---: | :---: |\n| 1 | 2 | 3 |")
        self.assertIn('<th class="cell-left">A</th>', html)
        self.assertIn('<th class="cell-right">B</th>', html)
        self.assertIn('<th class="cell-center">C</th>', html)

    def test_column_count_comes_from_header(self):
        html = evmd_to_html("@{table}\n| A | B |\n| 1 | 2 | 3 |\n| 4 |")
        self.assertIn("<td>1</td><td>2</td>", html)
        self.assertIn("<td>4</td><td></td>", html)

    def test_table_marker_without_rows_is_paragraph(self):
        html = evmd_to_html("@{table}\nno rows here")
        self.assertIn("<p>", html)
        self.assertNotIn("<table>", html)

    def test_table_cells_use_inline_syntax_and_escaping(self):
        html = evmd_to_html("@{table}\n| A |\n| **b** <x> |")
        self.assertIn("<td><strong>b</strong> &lt;x&gt;</td>", html)

    def test_table_row_limit(self):
        rows = ["@{table}", "| H |"] + [f"| {i} |" for i in range(MAX_TABLE_ROWS + 50)]
        html = evmd_to_html("\n".join(rows))
        self.assertEqual(html.count("<tr>"), MAX_TABLE_ROWS)

    def test_table_column_limit(self):
        header = "| " + " | ".join(f"c{i}" for i in range(MAX_TABLE_COLUMNS + 20)) + " |"
        html = evmd_to_html("@{table}\n" + header + "\n| 1 |")
        self.assertEqual(html.count("<th>"), MAX_TABLE_COLUMNS)


class FootnoteTests(unittest.TestCase):
    def test_definition_and_reference(self):
        html = evmd_to_html("text@{ref,n1}\n\n@{footnote,n1,the note}")
        self.assertIn('<sup class="footnote-ref"><a href="#fn-n1" id="fnref-n1">1</a></sup>', html)
        self.assertIn('<li id="fn-n1">the note', html)
        self.assertIn("footnote-backref", html)

    def test_undefined_reference_is_dropped(self):
        html = evmd_to_html("text@{ref,missing} end")
        self.assertNotIn("footnote", html)
        self.assertIn("text end", html)

    def test_duplicate_definitions_keep_first(self):
        html = evmd_to_html("@{ref,d}\n\n@{footnote,d,first}\n\n@{footnote,d,second}")
        self.assertIn("first", html)
        self.assertNotIn("second", html)

    def test_footnote_text_supports_inline_syntax(self):
        html = evmd_to_html("@{ref,f}\n\n@{footnote,f,**bold** note}")
        self.assertIn("<strong>bold</strong>", html)

    def test_footnote_limit(self):
        lines = [f"@{{footnote,f{i},text {i}}}" for i in range(MAX_FOOTNOTES + 20)]
        html = evmd_to_html("\n\n".join(lines))
        self.assertEqual(html.count('<li id="fn-'), MAX_FOOTNOTES)

    def test_malformed_footnote_is_plain_paragraph(self):
        html = evmd_to_html("@{footnote,}")
        self.assertIn("<p>", html)
        self.assertNotIn("footnotes", html)


class DeterminismTests(unittest.TestCase):
    """同一输入必须得到同一输出（缓存与回归的基础）。"""

    def test_repeated_renders_are_identical(self):
        source = (
            "@@@\ntitle = \"T\"\n@@@\n\n"
            "# H1\n\npara **b** `c` @{ref,x}\n\n"
            "```py\ncode\n```\n\n- a\n- b\n\n"
            "@{table}\n| A |\n| 1 |\n\n"
            "@{footnote,x,note}\n"
        )
        outputs = {parse_document(source)[1] for _ in range(5)}
        self.assertEqual(len(outputs), 1)

    def test_sample_article_renders(self):
        """仓库内的示例文章必须能正常渲染（内容兼容性回归）。"""
        sample = PROJECT_ROOT / "articles" / "2026-09-07-evmd-syntax-showcase.evmd"
        if not sample.exists():
            self.skipTest("sample article not present")
        meta, html = parse_document(sample.read_text(encoding="utf-8"), require_header=True)
        self.assertTrue(meta.get("title"))
        self.assertIn("<h", html)


if __name__ == "__main__":
    unittest.main()
