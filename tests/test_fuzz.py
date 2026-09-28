"""Fuzz / 性能测试：解析器与表单解析器在任意输入下不得崩溃、死循环或指数级变慢。

说明：
- 使用固定随机种子，保证失败可复现（不是"偶尔红"的随机测试）。
- 不引入任何第三方 fuzz 框架；只用标准库 random / string / time。
- 断言的是"健壮性契约"而不是具体输出：不抛未预期异常、耗时在阈值内、输出是字符串。
"""
import random
import string
import time
import unittest
from urllib.parse import parse_qs

from tests.support import PROJECT_ROOT, ElenvindTestCase  # 导入即完成 sys.path 设置

from elenvind.evmd_header import EvmdError, split_document
from elenvind.evmd_inline import MAX_INLINE_LENGTH, parse_inline, safe_url
from elenvind.evmd_parser import evmd_to_html

TOKENS = [
    "`", "**", "*", "_", "~~", "@{img,", "@{video,", "@{link,", "@{ref,",
    "@{footnote,", "@{table}", "|", "```", "#", "- ", "1. ", "> ", "---", "===",
    "\n", "\r\n", "  ", "\\", "<", ">", "&", '"', "'", "}", ",", "%", "\ue100",
    "\ue001", "\ue300", "中文", "🎉", "http://", "https://", "javascript:",
    "data:", "\x00", "\t", "x" * 100, " ",
]

SAFE_INPUTS = TOKENS + [t * 3 for t in TOKENS]


def random_text(rng, max_length=400, alphabet=None):
    alphabet = alphabet or SAFE_INPUTS
    parts = []
    size = 0
    while size < max_length:
        token = rng.choice(alphabet)
        parts.append(token)
        size += len(token)
    return "".join(parts)


def random_junk(rng, max_length=200):
    alphabet = string.printable + "中文🎉\ue100\ue001\ue300"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_length)))


class InlineFuzzTests(unittest.TestCase):
    def test_token_fuzz_never_raises(self):
        rng = random.Random(20260101)
        for _ in range(400):
            text = random_text(rng)
            with self.subTest(text=text[:60]):
                output = parse_inline(text)
                self.assertIsInstance(output, str)

    def test_junk_fuzz_never_raises(self):
        rng = random.Random(20260102)
        for _ in range(400):
            text = random_junk(rng)
            output = parse_inline(text)
            self.assertIsInstance(output, str)
            # 输出里不允许出现原始 HTML 标签（用户输入永远是文本）
            self.assertNotIn("<script", output)

    def test_pathological_inputs_are_fast(self):
        cases = {
            "backticks": "`" * 20_000,
            "asterisks": "*" * 20_000,
            "underscores": "_a" * 10_000,
            "tildes": "~~" * 10_000,
            "escapes": "\\*" * 10_000,
            "linkish": "@{link," * 5_000,
            "braces": "{}" * 10_000,
            "pua": "\ue100\ue101" * 5_000,
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                start = time.perf_counter()
                output = parse_inline(text)
                elapsed = time.perf_counter() - start
                self.assertIsInstance(output, str)
                self.assertLess(elapsed, 3.0, f"{name} took {elapsed:.3f}s")

    def test_very_long_single_line_is_bounded(self):
        text = "`x` " * (MAX_INLINE_LENGTH // 2)
        start = time.perf_counter()
        output = parse_inline(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 3.0)
        self.assertNotIn("<code>", output)   # 超限后按纯文本处理

    def test_safe_url_never_raises(self):
        rng = random.Random(20260103)
        inputs = [random_junk(rng, 60) for _ in range(500)]
        inputs += ["", " ", ":", "//", "http:", "http://", "http://a b",
                   "ht" + "t" * 100 + "p://x", "\x00", None, 1, [], {}]
        for value in inputs:
            with self.subTest(value=repr(value)[:40]):
                self.assertIsInstance(safe_url(value), str)


class BlockFuzzTests(unittest.TestCase):
    def test_document_fuzz_never_raises_unexpected(self):
        rng = random.Random(20260201)
        for _ in range(300):
            text = random_text(rng, max_length=600)
            try:
                output = evmd_to_html(text)
            except EvmdError:
                continue     # 明确的语法错误是允许的
            self.assertIsInstance(output, str)

    def test_header_fuzz(self):
        rng = random.Random(20260202)
        for _ in range(300):
            text = random_text(rng, max_length=200)
            try:
                meta, body = split_document(text)
            except EvmdError:
                continue
            self.assertIsInstance(body, str)
            self.assertTrue(meta is None or isinstance(meta, dict))

    def test_pathological_documents_are_fast(self):
        cases = {
            "many_fences": "```\n" * 5_000,
            "many_tables": ("@{table}\n| a |\n| b |\n" * 2_000),
            "many_footnotes": "@{footnote,f,text}\n\n" * 2_000,
            "many_images": "@{img, https://e.com/a.png, 50%}\n" * 5_000,
            "many_links": "@{link,https://e.com,x} " * 5_000,
            "deep_quotes": "> " * 2_000 + "text",
            "many_hr": "---\n\n" * 5_000,
            "long_line": "x" * 200_000,
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                start = time.perf_counter()
                output = evmd_to_html(text)
                elapsed = time.perf_counter() - start
                self.assertIsInstance(output, str)
                self.assertLess(elapsed, 5.0, f"{name} took {elapsed:.3f}s")

    def test_one_megabyte_document_renders_in_reasonable_time(self):
        """1 MB 正常文章（规格里的上限）必须在合理时间内渲染完。"""
        paragraph = ("这是一段正常的中文段落，包含 **粗体**、`代码` 与 "
                     "@{link,https://example.com,链接}。\n\n")
        text = paragraph * (1024 * 1024 // len(paragraph.encode("utf-8")))
        self.assertGreater(len(text.encode("utf-8")), 900_000)
        start = time.perf_counter()
        output = evmd_to_html(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 10.0, f"1MB document took {elapsed:.3f}s")
        self.assertEqual(output.count("<strong>"), text.count("**") // 2)


class FormParserFuzzTests(unittest.TestCase):
    def test_parse_qs_never_raises_on_random_input(self):
        rng = random.Random(20260301)
        for _ in range(500):
            text = random_junk(rng, 120)
            try:
                parsed = parse_qs(text, keep_blank_values=True)
            except ValueError:
                continue
            self.assertIsInstance(parsed, dict)
            for key, values in parsed.items():
                self.assertIsInstance(key, str)
                self.assertIsInstance(values, list)

    def test_fuzz_over_http_layer(self):
        """端到端：随机垃圾表单不得让应用返回 5xx。"""

        class _HttpFuzz(ElenvindTestCase):
            def runTest(self):
                rng = random.Random(20260302)
                for _ in range(40):
                    payload = random_junk(rng, 60)
                    body = payload.encode("utf-8", errors="replace")
                    response = self.app.raw_request(
                        "POST", "/login", b"",
                        [("host", "example.com"),
                         ("content-type", "application/x-www-form-urlencoded"),
                         ("content-length", str(len(body)))],
                        body=body)
                    self.assertLess(response.status, 500, f"payload={payload!r}")

        case = _HttpFuzz()
        case.setUp()
        try:
            case.runTest()
        finally:
            case.tearDown()

    def test_shipped_content_files_never_break_the_parser(self):
        """仓库内真实内容文件（示例文章 + 自定义页面）必须都能安全解析。"""
        candidates = list((PROJECT_ROOT / "articles").glob("*.evmd"))
        candidates += list((PROJECT_ROOT / "usrpages").glob("*.evmd"))
        self.assertTrue(candidates, "no shipped .evmd content found")
        for path in candidates:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                try:
                    html = evmd_to_html(text)
                except EvmdError:
                    html = ""   # 示例内容允许带文档头语法错误，但不得抛别的异常
                self.assertIsInstance(html, str)
                self.assertNotIn("<script", html.lower())


if __name__ == "__main__":
    unittest.main()
