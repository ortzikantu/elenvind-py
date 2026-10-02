"""样式表测试：配置优先 / 内置兜底 / 开关关闭 / 内置资源路由 / 模板漂移守卫。

对应设计决定：
- `[static].css` 有值 -> 用它（站内路径或绝对 URL）；
- 为空且 `use_builtin_css = true`（默认）-> 用缺省样式表
  （`elenvind/static/css/style.css`，URL 为 `/css/style.css`）；
- 为空且 `use_builtin_css = false` -> 不输出 `<link>`。

站点图标同理：`[static].favicon` 优先，为空则回落到
`elenvind/static/imgs/favicon.ico|png`（存在才输出 `<link>`）。
"""
import os
import re
import shutil
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT, ElenvindTestCase

from elenvind.core import assets
from elenvind.core.config import ConfigError, validate_config
from elenvind.core.templating import stylesheet_url

TEMPLATES_DIR = PROJECT_ROOT / "elenvind" / "templates"
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def declared_classes(css_text: str):
    """样式表里声明的所有类名（去掉注释，避免注释里的示例被算作声明）。"""
    body = CSS_COMMENT_RE.sub("", css_text)
    return set(re.findall(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)", body))


def template_classes():
    """所有模板里静态出现的类名（跳过含 Jinja 表达式的动态片段）。"""
    found = set()
    attr_re = re.compile(r"""class=["']([^"']*)["']""")
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
        for match in attr_re.finditer(text):
            for token in match.group(1).split():
                if "{" in token or "}" in token:
                    continue
                found.add(token)
    return found


def selector_block(css_body: str, selector: str) -> str:
    """取出形如 `selector { ... }` 的声明块内容（支持块嵌套）。

    比正则可靠：CSS 里花括号会嵌套（@media 内还有规则），
    按深度配对找闭合括号才不会截错。
    """
    marker = re.search(re.escape(selector) + r"\s*\{", css_body)
    if marker is None:
        raise AssertionError(f"selector not found: {selector}")
    start = marker.end() - 1          # 指向 "{"
    depth = 0
    for index in range(start, len(css_body)):
        char = css_body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css_body[start + 1:index]
    raise AssertionError(f"unbalanced braces after {selector}")


def colour_variables(declarations: str):
    """从声明块里挑出颜色类变量（值以 #/rgb/hsl 开头）。"""
    names = set()
    for name, value in re.findall(
            r"(--[_a-zA-Z][_a-zA-Z0-9-]*)\s*:\s*([^;]+);", declarations):
        if re.match(r"\s*(#|rgb|hsl)", value):
            names.add(name)
    return names


class StylesheetResolutionTests(ElenvindTestCase):
    """解析顺序：配置优先，内置兜底，开关可关闭。"""

    def test_configured_css_wins(self):
        self._config["static"] = {"css": "/assets/mine.css"}
        self._config["use_builtin_css"] = True
        self.assertEqual(stylesheet_url(), "/assets/mine.css")

    def test_absolute_url_wins(self):
        self._config["static"] = {"css": "https://cdn.example.com/site.css"}
        self.assertEqual(stylesheet_url(), "https://cdn.example.com/site.css")

    def test_blank_config_falls_back_to_default(self):
        for blank in ("", "   ", None):
            with self.subTest(css=blank):
                self._config["static"] = {"css": blank}
                self._config["use_builtin_css"] = True
                self.assertEqual(stylesheet_url(), assets.DEFAULT_CSS_PATH)

    def test_missing_static_table_falls_back_to_default(self):
        self._config.pop("static", None)
        self._config["use_builtin_css"] = True
        self.assertEqual(stylesheet_url(), assets.DEFAULT_CSS_PATH)

    def test_switch_off_means_no_stylesheet(self):
        self._config["static"] = {"css": ""}
        self._config["use_builtin_css"] = False
        self.assertEqual(stylesheet_url(), "")

    def test_switch_off_does_not_override_configured_css(self):
        """开关只管"为空时是否回落"，不该让显式配置失效。"""
        self._config["static"] = {"css": "/assets/mine.css"}
        self._config["use_builtin_css"] = False
        self.assertEqual(stylesheet_url(), "/assets/mine.css")

    def test_whitespace_only_config_is_treated_as_empty(self):
        self._config["static"] = {"css": "   \t  "}
        self._config["use_builtin_css"] = True
        self.assertEqual(stylesheet_url(), assets.DEFAULT_CSS_PATH)


class StylesheetRenderingTests(ElenvindTestCase):
    """真实渲染出来的 <link> 必须与解析规则一致。"""

    LINK_RE = re.compile(r'<link rel="stylesheet" href="([^"]*)"')

    def _link(self, path="/"):
        return self.LINK_RE.findall(self.app.request("GET", path).text)

    def test_configured_css_is_linked(self):
        self._config["static"] = {"css": "/assets/mine.css"}
        self.assertEqual(self._link(), ["/assets/mine.css"])

    def test_default_css_is_linked_when_config_blank(self):
        self._config["static"] = {"css": ""}
        self._config["use_builtin_css"] = True
        self.assertEqual(self._link(), [assets.DEFAULT_CSS_PATH])

    def test_no_link_when_disabled(self):
        self._config["static"] = {"css": ""}
        self._config["use_builtin_css"] = False
        self.assertEqual(self._link(), [])

    def test_default_css_link_is_same_origin(self):
        """内置样式必须同源，否则 CSP 的 style-src 'self' 会把它拦掉。"""
        self._config["static"] = {"css": ""}
        link = self._link()[0]
        self.assertTrue(link.startswith("/"), link)
        self.assertFalse(link.startswith("//"), link)

    def test_css_url_is_escaped_in_html(self):
        """配置里的 URL 进入 href 时必须转义（配置错误也不该造成注入）。"""
        self._config["static"] = {"css": "/a&b.css"}
        response = self.app.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn('href="/a&amp;b.css"', response.text)


class IconDowngradeTests(ElenvindTestCase):
    """站点图标与站标：配置优先、缺省兜底，缺省也没有时才不输出。"""

    ICON_RE = re.compile(r'<link rel="icon"[^>]*>')

    def test_configured_favicon_wins(self):
        self._config["static"] = {"css": "", "favicon": "/assets/icon.png"}
        page = self.app.request("GET", "/").text
        self.assertIn('<link rel="icon" href="/assets/icon.png">', page)

    def test_empty_favicon_falls_back_to_default_icon(self):
        """配置留空 -> 用 `elenvind/static/imgs/` 里的缺省图标。"""
        default = assets.default_icon_path()
        if default is None:
            self.skipTest("仓库未附带缺省图标")
        self._config["static"] = {"css": ""}
        page = self.app.request("GET", "/").text
        self.assertIn(f'<link rel="icon" href="{default}">', page)
        # 回退地址必须落在内置静态前缀下，且磁盘上真实存在
        self.assertTrue(default.startswith("" + "/"), default)
        self.assertTrue((assets.STATIC_DIR
                         / default[len("") + 1:]).is_file())

    def test_default_icon_is_reachable(self):
        """缺省图标的 URL 必须真的能取到（否则又是一个 404）。"""
        default = assets.default_icon_path()
        if default is None:
            self.skipTest("仓库未附带缺省图标")
        response = self.app.request("GET", default)
        self.assertEqual(response.status, 200, default)
        self.assertTrue(response.content_type.startswith("image/"),
                        response.content_type)

    def test_default_icon_file_is_valid_png(self):
        """缺省图标是手写 PNG：校验签名、IHDR 与块 CRC，防止提交坏文件。"""
        path = assets.default_icon_file()
        if path is None or path.suffix != ".png":
            self.skipTest("缺省图标不是 PNG")
        payload = path.read_bytes()
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        # 逐块校验 CRC（标准库就能做，不需要 Pillow）
        import struct
        import zlib
        position = 8
        tags = []
        while position < len(payload):
            length = struct.unpack(">I", payload[position:position + 4])[0]
            tag = payload[position + 4:position + 8]
            body = payload[position + 8:position + 8 + length]
            stored = struct.unpack(
                ">I", payload[position + 8 + length:position + 12 + length])[0]
            self.assertEqual(stored, zlib.crc32(tag + body) & 0xFFFFFFFF,
                             f"bad CRC in {tag!r}")
            tags.append(tag)
            position += 12 + length
        self.assertEqual(tags[0], b"IHDR")
        self.assertEqual(tags[-1], b"IEND")

    def test_the_actually_served_default_icon_is_structurally_valid(self):
        """真正被发出的缺省图标必须结构有效。

        回归：`test_default_icon_file_is_valid_png` 只在**缺省图标是 PNG** 时
        才跑，而 `DEFAULT_ICON_CANDIDATES` 把 `.ico` 排在前面，于是那个测试
        长期处于 skipped 状态 —— 实际被 `<link rel="icon">` 引用、
        真正有浏览器去取的 `.ico` 文件**从来没有被校验过**。

        这里按扩展名分派：`.png` 校验 PNG 块与 CRC，`.ico` 校验 ICONDIR 头
        与目录里每个图像的尺寸/偏移（不依赖任何第三方库）。
        """
        import struct

        path = assets.default_icon_file()
        self.assertIsNotNone(path, "仓库必须附带缺省图标")
        payload = path.read_bytes()

        if path.suffix == ".png":
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
            return

        self.assertEqual(path.suffix, ".ico", f"未知的图标格式：{path.name}")
        # ICONDIR: reserved(2) type(2) count(2)；type=1 表示图标
        reserved, kind, count = struct.unpack("<HHH", payload[:6])
        self.assertEqual(reserved, 0, "ICONDIR.reserved 必须为 0")
        self.assertEqual(kind, 1, "ICONDIR.type 必须为 1（图标）")
        self.assertGreater(count, 0, "图标目录里没有任何图像")
        self.assertGreaterEqual(len(payload), 6 + 16 * count,
                                "文件长度不足以容纳图标目录")

        # 每一项都有 IHDR 级的最小结构：尺寸、颜色数与数据偏移
        for index in range(count):
            offset = 6 + 16 * index
            entry = payload[offset:offset + 16]
            width = entry[0] or 256          # 0 表示 256
            height = entry[1] or 256
            data_size, data_offset = struct.unpack("<II", entry[8:16])
            with self.subTest(image=index):
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)
                self.assertGreater(data_size, 0, "图像数据长度为 0")
                self.assertLessEqual(data_offset + data_size, len(payload),
                                     "图像数据偏移超出了文件末尾")

    def test_logo_falls_back_to_configured_favicon(self):
        self._config["static"] = {"css": "", "favicon": "/assets/icon.png"}
        page = self.app.request("GET", "/").text
        self.assertIn('src="/assets/icon.png"', page)

    def test_logo_falls_back_to_default_icon(self):
        """logo 与 favicon 都留空 -> 站标用缺省图标（而不是只剩文字）。"""
        self._config["static"] = {"css": ""}
        page = self.app.request("GET", "/").text
        default = assets.default_icon_path()
        if default is None:
            self.skipTest("仓库未附带缺省图标")
        self.assertIn(f'src="{default}"', page)

    def test_configured_logo_beats_default_icon(self):
        """配置了 logo 时，站标用 logo（缺省图标仍然只作为 favicon 出现在 head）。"""
        self._config["static"] = {"css": "", "logo": "/assets/logo.png"}
        page = self.app.request("GET", "/").text
        self.assertIn('<img src="/assets/logo.png"', page)
        default = assets.default_icon_path()
        if default:
            self.assertNotIn(f'<img src="{default}"', page)

    def test_social_icon_empty_degrades_to_text(self):
        """社交条目没图标时退化为文字链接，而不是空的 <img>（会裂图）。"""
        self._config["params"]["social"] = [
            {"name": "Codeberg", "url": "https://codeberg.org/", "icon": ""},
        ]
        page = self.app.request("GET", "/").text
        self.assertIn("https://codeberg.org/", page)
        self.assertNotIn('<img src=""', page)

    def test_default_assets_live_under_package_static_dir(self):
        """缺省资产必须在包内 `elenvind/static/{css,imgs}/` 下。"""
        static = PROJECT_ROOT / "elenvind" / "static"
        self.assertTrue((static / "css" / "style.css").is_file())
        self.assertTrue((static / "imgs").is_dir())
        # 仓库根不再有 static/（那是给 Nginx 的部署目录，不是源码）
        self.assertFalse((PROJECT_ROOT / "static").exists())


class AssetRouteTests(ElenvindTestCase):
    """内置资源路由：正确的类型、缓存、ETag，且不泄漏目录。"""

    def test_default_css_is_served(self):
        response = self.app.request("GET", assets.DEFAULT_CSS_PATH)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.content_type.startswith("text/css"),
                        response.content_type)
        self.assertIn("--primary", response.text)
        self.assertIn(".header-title", response.text)

    def test_cache_headers(self):
        response = self.app.request("GET", assets.DEFAULT_CSS_PATH)
        self.assertIn("max-age=", response.header("cache-control") or "")
        self.assertTrue((response.header("cache-control") or "").startswith("public"))
        self.assertIsNotNone(response.header("etag"))

    def test_security_headers_still_present(self):
        response = self.app.request("GET", assets.DEFAULT_CSS_PATH)
        self.assertEqual(response.header("x-content-type-options"), "nosniff")
        self.assertIsNotNone(response.header("content-security-policy"))

    def test_conditional_request_returns_304(self):
        first = self.app.request("GET", assets.DEFAULT_CSS_PATH)
        etag = first.header("etag")
        self.assertTrue(etag)
        second = self.app.raw_request(
            "GET", assets.DEFAULT_CSS_PATH, b"",
            [("host", "example.com"), ("if-none-match", etag)])
        self.assertEqual(second.status, 304)
        self.assertEqual(second.body, b"")

    def test_stale_etag_returns_full_body(self):
        second = self.app.raw_request(
            "GET", assets.DEFAULT_CSS_PATH, b"",
            [("host", "example.com"), ("if-none-match", '"stale"')])
        self.assertEqual(second.status, 200)
        self.assertIn(b"--primary", second.body)

    def test_unknown_asset_is_404_not_500(self):
        for name in ("nope.css", "style.css.bak", "imgs/nope.png",
                     "css/nope.css", "nope/deep/path.png"):
            with self.subTest(name=name):
                response = self.app.request("GET", f"/{name}")
                self.assertEqual(response.status, 404, response.status)

    def test_asset_prefix_is_not_shadowed_by_page_fallback(self):
        """自定义页面不能抢占保留命名空间。"""
        self.write_page("css", "fake page that must not shadow /css/…")
        response = self.app.request("GET", assets.DEFAULT_CSS_PATH)
        self.assertEqual(response.status, 200)
        self.assertIn("--primary", response.text)

    def test_shipped_defaults_all_exist(self):
        """真正**附带**的缺省资产必须存在：样式表 + 至少一个图标。"""
        self.assertTrue(assets.stylesheet_file().is_file(),
                        "缺少缺省样式表 css/style.css")
        self.assertIsNotNone(assets.default_icon_path(),
                             "缺少缺省图标（imgs/favicon.ico 或 imgs/favicon.png）")

    def test_unlisted_asset_name_is_404(self):
        for name in ("nope.css", "secret.txt", "style.css.bak"):
            with self.subTest(name=name):
                response = self.app.request("GET", f"/{name}")
                self.assertEqual(response.status, 404, response.status)

    def test_post_to_asset_is_rejected(self):
        response = self.app.request("POST", assets.DEFAULT_CSS_PATH, form={})
        self.assertIn(response.status, (400, 405), response.status)


class GenericStaticServiceTests(ElenvindTestCase):
    """通用静态服务：`elenvind/static/` 下的任意文件都可按相对路径取到。

    安全边界是这个功能的全部风险所在，因此下面的穿越用例比"能取到文件"更重要。
    """

    def _write(self, relative, payload: bytes):
        """往内置静态根写一个临时文件，并在测试后把**新建的**目录一并清掉。

        不清理空目录会把 `fonts/deep/nested/`、`js/` 这类测试残留留在真实
        静态树里（曾经发生过），因此这里逐个记录新建目录并反向删除。
        """
        path = assets.STATIC_DIR / relative
        # 记录本次真正需要新建的目录，测试后按深度反向删除
        created = []
        parent = path.parent
        while parent != assets.STATIC_DIR and not parent.exists():
            created.append(parent)
            parent = parent.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

        def cleanup():
            path.unlink(missing_ok=True)
            for directory in created:          # 已按由深到浅的顺序记录
                try:
                    directory.rmdir()          # 只在为空时成功
                except OSError:
                    pass

        self.addCleanup(cleanup)
        return path

    def test_arbitrary_file_is_served_by_relative_path(self):
        self._write("imgs/logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        response = self.app.request("GET", "/imgs/logo.svg")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/svg+xml")
        self.assertIn("svg", response.text)

    def test_nested_subdirectories_work(self):
        self._write("fonts/deep/nested/x.woff2", b"\x00\x01\x02binary")
        response = self.app.request("GET", "/fonts/deep/nested/x.woff2")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "font/woff2")

    def test_binary_payload_round_trips(self):
        payload = bytes(range(256)) * 4
        self._write("imgs/blob.bin", payload)
        response = self.app.request("GET", "/imgs/blob.bin")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, payload)

    def test_content_type_for_css_and_js(self):
        self._write("css/extra.css", b"a{color:red}")
        self._write("js/app.js", b"console.log(1)")
        self.assertTrue(self.app.request("GET", "/css/extra.css")
                        .content_type.startswith("text/css"))
        self.assertTrue(self.app.request("GET", "/js/app.js")
                        .content_type.startswith("text/javascript"))

    def test_missing_file_is_404(self):
        response = self.app.request("GET", "/imgs/definitely-absent.png")
        self.assertEqual(response.status, 404)

    def test_directory_is_not_listed(self):
        """目录本身不可列出（避免变成文件清单）。"""
        for path in ("/imgs", "/imgs/", "/css", "/css/"):
            with self.subTest(path=path):
                self.assertEqual(self.app.request("GET", path).status, 404)


class StaticServiceTraversalTests(ElenvindTestCase):
    """穿越与信息泄露防护：越界形态一律 404，且不得泄漏仓库文件内容。"""

    HOSTILE_PATHS = [
        "/../config.toml",
        "/../../etc/passwd",
        "/..%2fconfig.toml",
        "/%2e%2e/config.toml",
        "/%2e%2e%2fconfig.toml",
        "/imgs/../../config.toml",
        "/imgs/%2e%2e/%2e%2e/config.toml",
        "/....//config.toml",
        "/..\\config.toml",
        "/imgs\\..\\..\\config.toml",
        "//etc/passwd",
        "/imgs/.hidden.png",
        "/.gitignore",
        "/.git/config",
        "/imgs/.env",
    ]

    def test_traversal_attempts_do_not_leak_repo_files(self):
        for path in self.HOSTILE_PATHS:
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertEqual(response.status, 404, response.status)
                body = response.text
                # config.toml / .gitignore / 系统文件的特征串都不得出现
                for marker in ("site_url", "admin_user_id", "__pycache__",
                               "root:x:", "password"):
                    self.assertNotIn(marker, body, f"{marker} leaked via {path}")

    def test_resolve_asset_rejects_outside_paths_directly(self):
        from elenvind.core.assets import resolve_asset
        for value in ("../config.toml", "..", "../../etc/passwd", "",
                      ".gitignore", ".git/config", "imgs/../../x",
                      "imgs/.hidden", "a/./../b", None):
            with self.subTest(value=value):
                self.assertIsNone(resolve_asset(value))

    def test_resolve_asset_accepts_normal_relative_paths(self):
        from elenvind.core.assets import resolve_asset
        self.assertIsNotNone(resolve_asset("css/style.css"))
        self.assertIsNotNone(resolve_asset("imgs/favicon.png"))
        # 冗余但无害的写法应收敛到同一文件
        self.assertEqual(resolve_asset("css/style.css"),
                         resolve_asset("css/./style.css"))

    def test_symlink_escape_is_rejected(self):
        """符号链接指向仓库外部时必须拒绝（不能只做字符串前缀比较）。"""
        link = assets.STATIC_DIR / "escape-link.txt"
        link.unlink(missing_ok=True)
        outside = PROJECT_ROOT / "config.toml"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不允许创建符号链接")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        from elenvind.core.assets import resolve_asset
        self.assertIsNone(resolve_asset("escape-link.txt"))
        self.assertEqual(self.app.request("GET", "/escape-link.txt").status,
                         404)

    def test_sibling_directory_with_static_prefix_is_rejected(self):
        """`static-evil/` 这类"前缀相同"的兄弟目录不能被字符串比较放过。"""
        from elenvind.core.assets import STATIC_DIR, resolve_asset
        sibling = STATIC_DIR.parent / "static-evil"
        sibling.mkdir(exist_ok=True)
        (sibling / "x.txt").write_text("secret", encoding="utf-8")
        try:
            self.assertIsNone(resolve_asset("../static-evil/x.txt"))
            self.assertEqual(
                self.app.request("GET", "/../static-evil/x.txt").status, 404)
        finally:
            shutil.rmtree(sibling, ignore_errors=True)


class DefaultStylesheetCoverageTests(unittest.TestCase):
    """漂移守卫：模板用到的类必须在内置样式表里有定义（只查静态类名）。"""

    #: 允许未覆盖的例外（附理由，避免守卫变成"改断言就过"）
    ALLOWED_WITHOUT_RULE = {
        # 评论操作按钮的类名来自 Feature 数据（action.css 取值 delete-link/restore-link），
        # 这里扫到的是模板里的表达式片段，不是真实类名
        "action.css",
        # 逻辑运算符被误当类名（模板已修为 |default 形式，此处兜底）
        "or",
    }

    def setUp(self):
        self.css_text = assets.stylesheet_file().read_text(encoding="utf-8")
        self.css_body = CSS_COMMENT_RE.sub("", self.css_text)
        self.declared = declared_classes(self.css_text)

    def test_default_css_exists_and_is_not_empty(self):
        self.assertTrue(assets.stylesheet_file().is_file())
        self.assertGreater(len(self.css_text), 3000)

    def test_every_template_class_has_a_rule(self):
        missing = sorted(
            name for name in template_classes() - self.declared
            if name not in self.ALLOWED_WITHOUT_RULE
        )
        self.assertEqual(missing, [], "模板用到的类在内置样式表里没有定义：\n"
                         + "\n".join(f"  .{name}" for name in missing))

    def test_no_undefined_css_variables(self):
        """var(--x) 引用的变量必须有定义（曾经 --card 是漏的）。"""
        defined = set(re.findall(r"(--[_a-zA-Z][_a-zA-Z0-9-]*)\s*:", self.css_body))
        used = set(re.findall(r"var\(\s*(--[_a-zA-Z][_a-zA-Z0-9-]*)", self.css_body))
        self.assertEqual(sorted(used - defined), [],
                         "内置样式表引用了未定义的 CSS 变量")

    def test_dark_theme_defines_every_colour_variable(self):
        """手动深色必须覆盖浅色定义的全部**颜色**变量。

        尺寸类变量（--content-width / --gutter / --radius）不需要重定义：
        它们不是颜色，也不受主题影响。
        """
        base = selector_block(self.css_body, ":root")
        dark = selector_block(self.css_body, 'html[data-theme="dark"]')
        colours = colour_variables(base)
        self.assertTrue(colours, "no colour variables found")
        self.assertEqual(sorted(colours - set(re.findall(
            r"(--[_a-zA-Z][_a-zA-Z0-9-]*)\s*:", dark))), [],
            "手动深色缺少这些颜色变量：")

    def test_system_dark_media_query_covers_the_same_variables(self):
        """跟随系统的暗色分支也必须完整覆盖同样的颜色变量。"""
        base = colour_variables(selector_block(self.css_body, ":root"))
        system_dark = selector_block(self.css_body, ":root:not([data-theme])")
        declared = set(re.findall(r"(--[_a-zA-Z][_a-zA-Z0-9-]*)\s*:", system_dark))
        self.assertEqual(sorted(base - declared), [],
                         "系统暗色分支缺少这些颜色变量：")

    def test_both_dark_paths_exist(self):
        """两条暗色路径都必须存在：跟随系统 + 手动切换。"""
        self.assertIn("prefers-color-scheme: dark", self.css_body)
        self.assertIn('html[data-theme="dark"]', self.css_body)

    def test_css_has_no_external_dependency(self):
        """内置样式必须完全自足：不引 @import、不引网络字体、不用网络图片。

        检查的是**去掉注释后的代码**，否则连"不引 @import"这句说明本身都会被判违规。
        """
        self.assertNotIn("@import", self.css_body)
        self.assertNotIn("url(http", self.css_body)
        self.assertNotIn("url(//", self.css_body)

    def test_header_brand_is_styled(self):
        """站标容器必须有排版规则（否则 logo 与站名会按行内基线错位）。"""
        self.assertIn("header-brand", self.declared)

    def test_form_message_classes_match_template_output(self):
        """提示条模板只会输出 .error / .success，两者都必须有样式。"""
        template = (TEMPLATES_DIR / "partials" / "form_message.html")
        text = template.read_text(encoding="utf-8")
        self.assertIn("'success' if message_kind == 'success' else 'error'", text)
        for name in ("error", "success"):
            self.assertIn(name, self.declared)

    def test_example_stylesheet_is_an_exact_copy(self):
        """`style.example.css` 必须与内置样式表逐字节一致。

        它是给"想完全接管外观"的人用的起点。如果允许它落后于内置样式，
        用户拷过去就会缺少内置样式后来修好的规则（例如 --card 变量、
        .header-brand 排版）。两者同源，这个测试负责防止漂移。

        修复方式：`copy elenvind/static/css/style.css style.example.css`
        """
        example = PROJECT_ROOT / "style.example.css"
        self.assertTrue(example.is_file(), "style.example.css is missing")
        self.assertEqual(
            example.read_bytes(), assets.stylesheet_file().read_bytes(),
            "style.example.css 与内置样式表不一致；请从内置样式表重新同步。")


class AssetUrlValidationTests(unittest.TestCase):
    """配置层字符集校验：挡住"经 HTML 实体解码后闭合 CSS url()"的注入。"""

    HOSTILE = [
        "/a');background:url(//evil/#",
        "/a'b.png",
        '/a"b.png',
        "/a b.png",
        "/a)b.png",
        "/a(b.png",
        "/a<b.png",
        "/a>b.png",
        "/a\\b.png",
        "https://cdn.example.com/a');x:url(//evil/#",
        "https://cdn.example.com/a b.png",
    ]

    @staticmethod
    def _base_config(**overrides):
        config = {"static": {}, "server": {}, "locale": "en", "title": "t",
                  "database": "x.db", "logging": {}}
        config.update(overrides)
        return config

    def test_hostile_asset_urls_are_rejected(self):
        for value in self.HOSTILE:
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as ctx:
                    validate_config(self._base_config(static={"css": value}))
                message = str(ctx.exception).lower()
                self.assertTrue(
                    any(word in message for word in
                        ("forbidden", "control", "protocol-relative", "must be")),
                    f"unhelpful rejection message: {ctx.exception}")

    def test_reasonable_asset_urls_are_accepted(self):
        for value in ("", "/style.css", "/assets/site.min.css",
                      "/static/img/a_b-c.webp", "https://cdn.example.com/site.css",
                      "https://cdn.jsdelivr.net/npm/x@1/y.css",
                      "/assets/a%20b.css"):
            with self.subTest(value=value):
                validate_config(self._base_config(static={"css": value}))

    def test_use_builtin_css_must_be_boolean(self):
        for bad in ("yes", 1, [], {}):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    validate_config(self._base_config(use_builtin_css=bad))


if __name__ == "__main__":
    unittest.main()
