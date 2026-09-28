"""EVMD 行级指令测试：@{img} / @{video} 的属性转义、协议白名单、并排换算、畸形输入。"""
import unittest

from tests.support import PROJECT_ROOT  # 导入即完成 sys.path 设置，同时用于示例文件断言

from elenvind.evmd_directive import (
    MAX_DIRECTIVE_LINE,
    img_row_html,
    match_modern_img,
    parse_directive_line,
    render_single_figure,
)
from elenvind.evmd_parser import evmd_to_html


class ImageMatchTests(unittest.TestCase):
    def test_plain_image(self):
        self.assertEqual(match_modern_img("@{img, https://e.com/a.png}"),
                         ("https://e.com/a.png", None))

    def test_percentage_image(self):
        self.assertEqual(match_modern_img("@{img, https://e.com/a.png, 30%}"),
                         ("https://e.com/a.png", 30))

    def test_percentage_bounds(self):
        self.assertIsNone(match_modern_img("@{img, https://e.com/a.png, 4%}"))
        self.assertIsNone(match_modern_img("@{img, https://e.com/a.png, 101%}"))
        self.assertEqual(match_modern_img("@{img, https://e.com/a.png, 5%}")[1], 5)
        self.assertEqual(match_modern_img("@{img, https://e.com/a.png, 100%}")[1], 100)

    def test_inline_occurrence_is_not_a_directive(self):
        self.assertIsNone(match_modern_img("text @{img, https://e.com/a.png}"))
        self.assertIsNone(match_modern_img("@{img, https://e.com/a.png} trailing"))

    def test_legacy_arguments_are_not_recognized(self):
        self.assertIsNone(match_modern_img("@{img, https://e.com/a.png, left}"))
        self.assertIsNone(match_modern_img("@{img, https://e.com/a.png, 500px}"))

    def test_overlong_line_is_ignored(self):
        line = "@{img, https://e.com/" + "a" * (MAX_DIRECTIVE_LINE + 100) + "}"
        self.assertIsNone(match_modern_img(line))


class ImageRenderTests(unittest.TestCase):
    def test_single_figure(self):
        html = render_single_figure("https://e.com/a.png")
        self.assertEqual(html, '<figure class="img-single"><img src="https://e.com/a.png" alt=""></figure>')

    def test_dangerous_scheme_is_rejected(self):
        for url in ("javascript:alert(1)", "data:image/svg+xml,<svg onload=alert(1)>",
                    "vbscript:x", "file:///etc/passwd"):
            with self.subTest(url=url):
                html = render_single_figure(url)
                self.assertNotIn("src=\"javascript", html)
                self.assertNotIn("<svg", html)
                self.assertEqual(html, "<p>Invalid image URL</p>")

    def test_attribute_injection_is_escaped_or_rejected(self):
        # 带空格/引号的 URL 被 safe_url 直接拒绝
        rejected = render_single_figure('https://e.com/a.png" onerror="alert(1)')
        self.assertEqual(rejected, "<p>Invalid image URL</p>")

        # 域名后紧跟引号（无空白）仍会被转义后放进属性，不会逃出属性
        escaped = render_single_figure('https://e.com/a.png"onerror="alert(1)')
        self.assertNotIn('onerror="alert(1)"', escaped)
        self.assertIn("&quot;", escaped)
        self.assertEqual(escaped.count("<img"), 1)

    def test_row_layout_percentages(self):
        html = img_row_html([("https://e.com/a.png", 50), ("https://e.com/b.png", 50)])
        self.assertIn('<div class="img-row">', html)
        self.assertEqual(html.count("<figure"), 2)
        self.assertIn("calc((100% - 1 * var(--img-gap))", html)

    def test_row_splits_when_shares_exceed_100(self):
        items = [("https://e.com/a.png", 34), ("https://e.com/b.png", 34),
                 ("https://e.com/c.png", 34)]
        html = img_row_html(items)
        self.assertEqual(html.count('<div class="img-row">'), 2)

    def test_single_item_uses_share_directly(self):
        html = img_row_html([("https://e.com/a.png", 60)])
        self.assertIn("flex-basis:60%", html)

    def test_row_rejects_unsafe_urls(self):
        html = img_row_html([("javascript:alert(1)", 50), ("https://e.com/b.png", 50)])
        self.assertNotIn("javascript", html)
        self.assertEqual(html.count("<figure"), 1)


class VideoDirectiveTests(unittest.TestCase):
    def test_video_renders(self):
        self.assertEqual(parse_directive_line("@{video, https://e.com/v.mp4}"),
                         '<video src="https://e.com/v.mp4" controls></video>')

    def test_video_rejects_unsafe_scheme(self):
        self.assertEqual(parse_directive_line("@{video, javascript:alert(1)}"),
                         "<p>Invalid video URL</p>")

    def test_non_video_lines_return_none(self):
        for line in ("plain text", "@{img, https://e.com/a.png}", "> quoted",
                     "@{video}", "@{video,}"):
            with self.subTest(line=line):
                self.assertIsNone(parse_directive_line(line))

    def test_video_attribute_injection_is_escaped(self):
        # 无空白但含引号的 URL：必须被转义，不能逃出 src 属性
        html = parse_directive_line('@{video, https://e.com/v.mp4"onload="alert(1)}')
        self.assertNotIn('onload="alert(1)"', html)
        self.assertIn("&quot;", html)

    def test_video_url_with_spaces_is_rejected(self):
        self.assertEqual(parse_directive_line('@{video, https://e.com/v.mp4" x="y}'),
                         "<p>Invalid video URL</p>")


class DirectiveInDocumentTests(unittest.TestCase):
    def test_image_rows_in_document(self):
        html = evmd_to_html(
            "@{img, https://e.com/a.png, 50%}\n@{img, https://e.com/b.png, 50%}")
        self.assertIn('<div class="img-row">', html)
        self.assertEqual(html.count("<img"), 2)

    def test_plain_image_breaks_row(self):
        html = evmd_to_html(
            "@{img, https://e.com/a.png, 50%}\n@{img, https://e.com/full.png}\n"
            "@{img, https://e.com/b.png, 50%}")
        self.assertIn("img-single", html)
        self.assertEqual(html.count('<div class="img-row">'), 2)

    def test_document_has_no_raw_user_html(self):
        payload = '@{img, https://e.com/a.png" onerror="alert(1)}\n\n<script>alert(2)</script>'
        html = evmd_to_html(payload)
        self.assertNotIn('onerror="alert(1)"', html)
        self.assertNotIn("<script", html)

    def test_image_run_is_chunked_at_limit(self):
        """连续图片行超过单次收集上限时按批处理，不丢内容也不无限累积。"""
        from elenvind.evmd_parser import MAX_IMAGE_RUN

        total = MAX_IMAGE_RUN + 50
        lines = [f'@{{img, https://e.com/{i}.png, 100%}}' for i in range(total)]
        html = evmd_to_html("\n".join(lines))
        self.assertEqual(html.count("<img"), total)

    def test_shipped_sample_article_directives_render_safely(self):
        """仓库内示例文章的图片/视频指令必须渲染成安全 HTML（内容兼容性回归）。"""
        sample = PROJECT_ROOT / "articles" / "2026-09-07-evmd-syntax-showcase.evmd"
        if not sample.exists():
            self.skipTest("sample article not present")
        html = evmd_to_html(sample.read_text(encoding="utf-8"))
        self.assertTrue("<img" in html or "<video" in html,
                        "sample article no longer exercises image/video directives")
        self.assertNotIn("javascript:", html.lower())
        self.assertNotIn("<script", html.lower())
        # 所有渲染出的 src 都必须是白名单协议
        for chunk in html.split('src="')[1:]:
            url = chunk.split('"', 1)[0]
            self.assertTrue(url.startswith(("http://", "https://")),
                            f"non-whitelisted src rendered: {url!r}")


if __name__ == "__main__":
    unittest.main()
