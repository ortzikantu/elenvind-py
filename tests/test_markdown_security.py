"""Markdown / XSS 安全回归（Phase 4-D）。

补齐审计发现的测试缺口（此前只有少量 payload，且部分 payload 实际上被
**预转义阶段**拦下，从未真正打到净化器的关键分支）：

1. 协议相对 URL（`//host/x`）：不写 `http(s)://` 也能引入任意第三方资源；
2. scheme 混淆（HTML 实体、TAB、百分号、大小写、前后空白）；
3. 属性剥离：`style=`、`on*=`（直接打到净化器，而不是靠预转义）；
4. 危险容器连内容删除：`script`/`iframe`/`object`/`svg`；
5. 未闭合的危险容器会吞掉后文（已知行为，锁住它免得变成"内容泄漏"）；
6. 缩进误判边界：被当成代码块的行不做预转义，原始 HTML 真身进入净化器 ——
   危险属性仍必须被剥掉，协议相对 URL 仍必须被拒；
7. `poster` / `source@src` 这类较少见的属性同样走 URL 白名单。
"""
import unittest

from elenvind.core.markdown import render_markdown, sanitize_html


class ProtocolRelativeUrlTests(unittest.TestCase):
    """`//evil.test/x` 必须被丢弃：它会继承页面协议去请求任意第三方。"""

    def test_markdown_link_and_image_reject_protocol_relative(self):
        link = render_markdown("[x](//evil.test/page)")
        image = render_markdown("![i](//evil.test/pixel.png)")
        self.assertNotIn("//evil.test", link)
        self.assertNotIn("//evil.test", image)

    def test_video_directive_rejects_protocol_relative(self):
        """`@video(//host/x)` 不是合法媒体地址：指令不生效（只留可见文本）。"""
        rendered = render_markdown("@video(//evil.test/x.mp4)")
        self.assertNotIn("<video", rendered)
        self.assertNotIn('src="//evil.test', rendered)

    def test_single_slash_site_relative_paths_still_work(self):
        """站内路径不受影响（正确的写法是单斜杠）。"""
        rendered = render_markdown("![i](/imgs/logo.png)")
        self.assertIn('src="/imgs/logo.png"', rendered)


class SchemeObfuscationTests(unittest.TestCase):
    """混淆过的危险 scheme 一律不得进入 href/src。"""

    OBFUSCATED = (
        '<a href="jav&#x61;script:alert(1)">x</a>',
        '<a href="jav&#97;script:alert(1)">x</a>',
        '<a href="java\tscript:alert(1)">x</a>',
        '<a href="  javascript:alert(1)">x</a>',
        '<a href="JaVaScRiPt:alert(1)">x</a>',
        '<a href="vbscript:msgbox(1)">x</a>',
        '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    )

    def test_dangerous_schemes_are_dropped(self):
        for payload in self.OBFUSCATED:
            with self.subTest(payload=payload):
                cleaned = sanitize_html(payload)
                self.assertNotIn("javascript:", cleaned.lower())
                self.assertNotIn("vbscript:", cleaned.lower())
                self.assertNotIn("data:text/html", cleaned.lower())

    def test_percent_encoded_scheme_is_not_a_valid_scheme(self):
        """`java%73cript:` 不是合法 scheme（浏览器不会解码后再判定）。

        净化器把它当相对路径保留 —— 这里锁住"它永远不会变成 `javascript:`"。
        """
        cleaned = sanitize_html('<a href="java%73cript:alert(1)">x</a>')
        self.assertNotIn("javascript:", cleaned.lower())

    def test_allowed_schemes_survive(self):
        for url in ("https://example.com/x", "http://example.com/x", "mailto:a@b.c"):
            with self.subTest(url=url):
                self.assertIn(url, sanitize_html(f'<a href="{url}">x</a>'))


class AttributeStrippingTests(unittest.TestCase):
    """直接打净化器（不经预转义）：危险属性必须被剥掉。"""

    def test_style_and_event_handlers_are_stripped(self):
        cleaned = sanitize_html('<img src="/a.png" style="color:red" onerror="alert(1)">')
        self.assertIn('src="/a.png"', cleaned)
        self.assertNotIn("style", cleaned)
        self.assertNotIn("onerror", cleaned)

    def test_click_handler_is_stripped(self):
        cleaned = sanitize_html('<p onclick="alert(1)">t</p>')
        self.assertNotIn("onclick", cleaned)
        self.assertIn("t", cleaned)

    def test_attr_list_extension_cannot_smuggle_handlers(self):
        cleaned = render_markdown('text\n{: style="color:red" onmouseover="alert(1)" }')
        self.assertNotIn("style=", cleaned)
        self.assertNotIn("onmouseover", cleaned)

    def test_video_poster_and_source_use_the_url_whitelist(self):
        poster = sanitize_html('<video src="/v.mp4" poster="javascript:alert(1)"></video>')
        self.assertIn('src="/v.mp4"', poster)
        self.assertNotIn("javascript:", poster.lower())
        source = sanitize_html('<video><source src="data:text/html,x" type="video/mp4"></video>')
        self.assertNotIn("data:text/html", source.lower())


class DangerousContainerTests(unittest.TestCase):
    DROPPED = (
        '<iframe src="//evil.test"></iframe>after',
        '<object data="//evil.test"></object>after',
        '<svg><script>alert(1)</script></svg>after',
        '<style>body{background:url(//evil.test)}</style>after',
        '<template><script>alert(1)</script></template>after',
        '<noscript><img src="//evil.test"></noscript>after',
    )

    def test_container_and_content_are_removed(self):
        for payload in self.DROPPED:
            with self.subTest(payload=payload):
                cleaned = sanitize_html(payload)
                self.assertIn("after", cleaned, "容器之外的内容必须保留")
                self.assertNotIn("evil.test", cleaned)

    def test_unclosed_drop_tag_swallows_the_rest(self):
        """未闭合的危险容器：其后内容一并丢弃（宁可少显示，不可泄漏/执行）。"""
        cleaned = sanitize_html("<script>alert(1)</script>visible")
        self.assertNotIn("alert(1)", cleaned)
        self.assertNotIn("<script", cleaned)

    def test_raw_html_in_normal_text_is_escaped(self):
        """正文里的原始 HTML 被**转义成可见文本**（不是被净化器解析）。"""
        cleaned = render_markdown("<b>bold</b> and <img src=x onerror=alert(1)>")
        self.assertIn("&lt;b&gt;", cleaned)
        self.assertIn("&lt;img", cleaned)
        self.assertNotIn("<b>", cleaned)
        self.assertNotIn("<img", cleaned)


class IndentationBoundaryTests(unittest.TestCase):
    """缩进误判（被判为代码块的行跳过预转义）下的边界。

    审计发现：`- i\\n\\n    <video ...>` 这类形态会被 `_code_block_flags` 判成代码，
    于是原始 HTML 以"允许标签的真身"进入净化器。净化器仍然必须剥掉危险属性、
    拒绝协议相对与外链 scheme —— 这几条就是该边界的回归锁。
    """

    PAYLOAD = ('- item\n\n    <video src="//evil.test/x.mp4" controls '
               'onerror="alert(1)"></video>')

    def test_boundary_still_strips_handlers_and_remote_urls(self):
        cleaned = render_markdown(self.PAYLOAD)
        self.assertNotIn("onerror", cleaned)
        self.assertNotIn("//evil.test", cleaned, "协议相对 URL 必须被拒绝")

    def test_boundary_drops_dangerous_attributes_on_links(self):
        cleaned = render_markdown('- item\n\n    <a href="javascript:alert(1)" onclick="x">c</a>')
        self.assertNotIn("javascript:", cleaned.lower())
        self.assertNotIn("onclick", cleaned)
        self.assertIn("c", cleaned)

    def test_boundary_strips_style_attributes(self):
        cleaned = render_markdown('- item\n\n    <div class="x" style="color:red">t</div>')
        self.assertNotIn("style=", cleaned)
        self.assertIn('class="x"', cleaned)


class FenceAndCommentTests(unittest.TestCase):
    def test_fenced_payloads_stay_inert(self):
        """围栏内是**可见文本**：标签被转义，不会变成元素。"""
        for payload in ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>"):
            with self.subTest(payload=payload):
                cleaned = render_markdown(f"```html\n{payload}\n```")
                self.assertNotIn("<script", cleaned)
                self.assertNotIn("<img", cleaned)
                self.assertIn("&lt;", cleaned)

    def test_html_comment_cannot_hide_a_payload(self):
        cleaned = render_markdown("<!-- <script>alert(1)</script> -->")
        self.assertNotIn("<script", cleaned)


if __name__ == "__main__":
    unittest.main()
