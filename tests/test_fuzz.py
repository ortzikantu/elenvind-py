"""Fuzz / 健壮性测试：Markdown 渲染与 front matter 解析对任意输入都必须安全。

（"内容渲染"的唯一入口是 `core.markdown.render_markdown`，因此 fuzz 目标
就是它：无论输入什么，输出都不得包含可执行内容。）

判定方式是**输出层面**的：无论输入什么，渲染结果里都不能出现
可执行的脚本/事件属性/危险协议。随机用例使用固定种子，失败可复现。
"""
import random
import unittest
from html.parser import HTMLParser

from tests.support import PROJECT_ROOT

from elenvind.core.content import ContentError, parse_document
from elenvind.core.markdown import render_markdown

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.test/x.js></SCRIPT>",
    "<img src=x onerror=alert(1)>",
    "<img src=x onerror=alert(1) width=1>",
    "<svg/onload=alert(1)>",
    "<body onload=alert(1)>",
    "<iframe src=javascript:alert(1)></iframe>",
    '<a href="javascript:alert(1)">x</a>',
    "[x](javascript:alert(1))",
    "![x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[x](vbscript:msgbox(1))",
    "[x](file:///etc/passwd)",
    "<style>@import '//evil.test/x.css';</style>",
    "<object data=x></object>",
    "<embed src=x>",
    "<form action=//evil.test><input name=x></form>",
    "<meta http-equiv=refresh content='0;url=//evil.test'>",
    "<base href=//evil.test>",
    "<!--<script>alert(1)</script>-->",
    "<?php echo 1; ?>",
    '<div style="background:url(javascript:alert(1))">x</div>',
    "<math><mtext><script>alert(1)</script></mtext></math>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "&#60;script&#62;alert(1)&#60;/script&#62;",
    '"><script>alert(1)</script>',
    "'>\"><img src=x onerror=alert(1)>",
]

FENCE = "```"

DANGEROUS_TAGS = frozenset({"script", "iframe", "object", "embed", "style", "svg",
                            "math", "form", "input", "button", "template",
                            "noscript", "base", "meta", "link"})
URL_ATTRS = frozenset({"href", "src", "poster", "action", "cite", "background",
                       "data", "formaction", "style"})
SAFE_URL_PREFIXES = ("http://", "https://", "mailto:", "/", "#", "?")
#: URL 里出现 scheme 才算危险；没有 scheme 的相对路径即使含 ":" 也安全
SCHEME_RE = __import__("re").compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*):")


class _Audit(HTMLParser):
    """把渲染结果当 DOM 解析：只有**真实元素/属性**才算风险。

    字符串包含判断会把安全的转义文本（`&lt;script&gt;`）误报为漏洞，
    因此这里必须看解析后的结构。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = set()
        self.events = []
        self.bad_urls = []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for name, value in attrs:
            name = (name or "").lower()
            if name.startswith("on"):
                self.events.append((tag, name, value))
            if name in URL_ATTRS and value:
                lowered = value.strip().lower()
                if lowered.startswith(SAFE_URL_PREFIXES):
                    continue
                match = SCHEME_RE.match(lowered)
                if match and match.group(1) not in ("http", "https", "mailto"):
                    self.bad_urls.append((tag, name, value))

    handle_startendtag = handle_starttag


def audit(html: str) -> _Audit:
    parser = _Audit()
    parser.feed(str(html))
    parser.close()
    return parser


class MarkdownFuzzTests(unittest.TestCase):
    """定向 + 随机输入：输出中不得出现可执行内容。"""

    def assert_clean(self, payload):
        output = str(render_markdown(payload))
        result = audit(output)
        for label, items in (("dangerous tag", result.tags & DANGEROUS_TAGS),
                             ("event attribute", result.events),
                             ("dangerous url", result.bad_urls)):
            self.assertEqual(items, set() if label == "dangerous tag" else [],
                             f"{label} survived {payload[:80]!r}: {items}")
        return output

    def test_known_payloads(self):
        for payload in XSS_PAYLOADS:
            with self.subTest(payload=payload):
                self.assert_clean(payload)

    def test_payloads_inside_fenced_code_stay_text(self):
        for payload in XSS_PAYLOADS[:12]:
            with self.subTest(payload=payload):
                self.assert_clean(f"{FENCE}\n{payload}\n{FENCE}")

    def test_random_ascii_fuzz_is_total(self):
        """随机字符串不能让渲染崩溃，也不能产出危险标记。"""
        rng = random.Random(20260730)
        alphabet = "<>&\"'`/*\\[](){}!#-_=+ \tabcxyz019:;@|~^%$,.?!\n"
        for _ in range(400):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
            with self.subTest(text=text[:60]):
                self.assert_clean(text)

    def test_random_structure_fuzz(self):
        """随机拼接的 Markdown 结构（表格/标题/列表/链接/代码）同样安全。"""
        rng = random.Random(42)
        pieces = ["# H", "## H2", "- item", "1. num", "> quote", "---", "***",
                  "| a | b |", "|---|---|", "text", "", "**b**", "`c`",
                  "[l](https://e.com)", "![i](https://e.com/a.png)", "    indented",
                  "<script>alert(1)</script>", "[^1]", "[^1]: note"]
        for _ in range(200):
            text = "\n".join(rng.choice(pieces) for _ in range(rng.randint(1, 25)))
            with self.subTest(text=text[:60]):
                self.assert_clean(text)

    def test_deeply_nested_input_terminates(self):
        for depth in (10, 100, 500):
            with self.subTest(depth=depth):
                self.assert_clean("[" * depth + "x" + "]" * depth)
                self.assert_clean(">" * depth + " quote")
                self.assert_clean("*" * depth + " text")

    def test_pathological_sizes_terminate(self):
        self.assert_clean("a" * 200_000)
        self.assert_clean("a\n" * 20_000)
        self.assert_clean("|" * 5_000)
        self.assert_clean("[" * 20_000)
        self.assert_clean("`" * 10_000)

    def test_non_string_and_none(self):
        self.assertEqual(str(render_markdown(None)), "")
        with self.assertRaises(TypeError):
            render_markdown(123)


class FrontMatterFuzzTests(unittest.TestCase):
    """front matter 解析对畸形输入必须"明确报错"，而不是崩溃或静默错值。"""

    def test_random_toml_fuzz_never_crashes_unexpectedly(self):
        rng = random.Random(7)
        alphabet = "abc\"'=\n[]{},.0123456789#-+ \t"
        for _ in range(300):
            body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
            text = f"+++\n{body}\n+++\nrest"
            try:
                parse_document(text)
            except ContentError:
                pass                      # 预期：明确的内容错误
            except Exception as exc:      # noqa: BLE001
                self.fail(f"unexpected {type(exc).__name__} for {body[:60]!r}: {exc}")

    def test_missing_and_extra_markers(self):
        self.assertEqual(parse_document("no markers")[0], {})
        with self.assertRaises(ContentError):
            parse_document('+++\ntitle = "x"')
        meta, body = parse_document('+++\ntitle = "x"\n+++\n+++\nmore')
        self.assertEqual(meta["title"], "x")
        self.assertIn("+++", body)        # 后续的 +++ 属于正文

    def test_shipped_content_parses(self):
        """仓库自带内容必须全部可解析（防止提交坏文件）。"""
        files = list((PROJECT_ROOT / "articles").glob("*.md"))
        files += list((PROJECT_ROOT / "custom_pages").glob("*.md"))
        self.assertTrue(files, "no shipped .md content found")
        for path in files:
            with self.subTest(path=path.name):
                metadata, body = parse_document(path.read_text(encoding="utf-8"))
                if path.parent.name == "articles":
                    self.assertIn("title", metadata, path.name)
                    self.assertTrue(str(metadata["title"]).strip(), path.name)
                self.assertNotIn("<script", str(render_markdown(body)).lower(),
                                 path.name)


if __name__ == "__main__":
    unittest.main()
