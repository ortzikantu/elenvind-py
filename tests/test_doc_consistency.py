"""文档一致性守卫：文档里的"具体断言"必须与代码一致。

为什么需要：本轮审计发现文档里 20+ 处与代码不符，而**没有测试会因此变红** ——
文档漂移是静默的。这里把最容易漂、最误导人的几类断言锁住：

1. 会话寿命（绝对/空闲天数）—— 写错会让运维按错误预期排查"为什么被踢"；
2. `max_comment_depth = 0` —— 文档曾说"0 = 不限层级"，实际会拒绝启动；
3. `admin_user_id` 的缺省/取消/非法语义；
4. 引用的脚本路径必须真实存在（曾经写 `scripts/smoke_driver.py`，实际在根目录）；
5. 引用的符号必须真实存在（曾经引用 `safe_css_url()` / `MAX_BODY_SIZE` / `EVMD`）；
6. `[static]` 的默认图标/logo 与磁盘上的文件一致；
7. 同一份 config 键不得在 `config.toml` / `config.example.toml` 之间
   "一个有、一个没有"（结构性缺漏，而非取值不同）。
"""
import re
import tomllib
from pathlib import Path

from tests.support import PROJECT_ROOT, ElenvindTestCase

ROOT = Path(PROJECT_ROOT)
DOCS = ("README.md", "docs/CONFIGURATION.md", "docs/DEPLOYMENT.md",
        "docs/OPS_GUIDE.md", "docs/development/features.md",
        "docs/nginx.conf.example", "config.example.toml")


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


class SessionLifetimeDocsTests(ElenvindTestCase):
    def test_documented_session_days_match_code(self):
        """文档里写的绝对/空闲天数必须是代码里的默认值。"""
        from elenvind.core.db_session import DEFAULT_ABSOLUTE_DAYS, DEFAULT_IDLE_DAYS

        combined = read("docs/CONFIGURATION.md") + read("docs/OPS_GUIDE.md")
        self.assertIn(str(DEFAULT_ABSOLUTE_DAYS), combined,
                      "文档没有提到真实的绝对过期天数")
        self.assertIn(str(DEFAULT_IDLE_DAYS), combined,
                      "文档没有提到真实的空闲过期天数")
        # 不得再出现"登录会话 7 天过期"这类旧说法
        self.assertNotRegex(
            combined, r"会话\s*7\s*天",
            "文档仍在说会话 7 天过期（真实值是 30 天绝对 / 15 天空闲）")


class MaxCommentDepthDocsTests(ElenvindTestCase):
    #: 否定标志：出现这些说明作者正是在**否认**"0 = 不限"，属于正确表述
    NEGATIONS = ("不是", "不会", "没有", "拒绝", "非法", "无效")

    def test_docs_do_not_claim_zero_means_unlimited(self):
        """`max_comment_depth = 0` 会拒绝启动，文档不得声称它表示不限层级。

        判定在**整个文件**上做（不是单行）：说法可能写在键的说明行，也可能写在
        紧随其后的段落里（例如"设为 `0` 表示不限制层级"单独成段），
        只扫"含 max_comment_depth 的行"会漏掉后者。

        写法很多（"设为 0 表示不限制"、"0 = 不限层级"…），所以放宽成
        "0 附近出现 不限制/不限"；同一窗口里有"不是 / 拒绝 / 没有"等否定词时
        跳过 —— 那正是在说明"0 不等于不限"，属于我们要的那句话。
        """
        pattern = re.compile(r"0[^0-9].{0,14}?不(限制|限)")
        for name in ("docs/CONFIGURATION.md", "config.example.toml"):
            text = read(name)
            if "max_comment_depth" not in text:
                # 这两个文件都必须说明这个键，缺了本身就是文档退化
                self.fail(f"{name} 完全没有提到 max_comment_depth")
            for lineno, line in enumerate(text.splitlines(), 1):
                match = pattern.search(line)
                if not match:
                    continue
                window = line[max(0, match.start() - 6):match.start() + 24]
                if any(word in window for word in self.NEGATIONS):
                    continue
                self.fail(f"{name}:{lineno} 声称 0 表示不限层级：{line.strip()}")

    def test_code_range_rejects_zero(self):
        """与上一条互为印证：代码确实拒绝 0。"""
        from elenvind.core.config import ConfigError, validate_config
        from elenvind.core import config as config_module

        saved = config_module.config.copy()
        config_module.config.clear()
        config_module.config.update({
            "title": "T", "locale": "en", "max_comment_depth": 0,
            "server": {"host": "127.0.0.1", "port": 6789},
        })
        try:
            with self.assertRaises(ConfigError):
                validate_config()
        finally:
            config_module.config.clear()
            config_module.config.update(saved)


class AdminUserIdDocsTests(ElenvindTestCase):
    def test_docs_describe_the_real_admin_semantics(self):
        """缺省 -> 1；`null` -> 无管理员；非法值 -> 拒绝启动。"""
        text = read("docs/CONFIGURATION.md")
        self.assertIn("admin_user_id", text)
        # 曾经的错误说法："缺失或非法时视为无管理员"
        self.assertNotRegex(text, r"缺失或非法时视为")

    def test_removing_the_key_keeps_the_default_admin(self):
        from elenvind.core.config import validate_config
        from elenvind.core import config as config_module

        saved = config_module.config.copy()
        config_module.config.clear()
        config_module.config.update({
            "title": "T", "locale": "en",
            "server": {"host": "127.0.0.1", "port": 6789},
        })
        try:
            validate_config()          # 缺省必须通过（取默认 1）
        finally:
            config_module.config.clear()
            config_module.config.update(saved)

    def test_null_means_no_admin_and_is_accepted(self):
        from elenvind.core.config import validate_config
        from elenvind.core import config as config_module

        saved = config_module.config.copy()
        config_module.config.clear()
        config_module.config.update({
            "title": "T", "locale": "en", "admin_user_id": None,
            "server": {"host": "127.0.0.1", "port": 6789},
        })
        try:
            validate_config()
        finally:
            config_module.config.clear()
            config_module.config.update(saved)


class DocumentedPathTests(ElenvindTestCase):
    #: 文档里出现过的、必须真实存在的路径
    REQUIRED_PATHS = ("smoke_driver.py", "run.py", "config.example.toml",
                      "docs/nginx.conf.example", "elenvind/static/css/style.css",
                      "elenvind/static/imgs/favicon.ico",
                      "elenvind/static/imgs/logo.png")

    def test_required_paths_exist(self):
        for relative in self.REQUIRED_PATHS:
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).exists(), f"{relative} 不存在")

    def test_no_reference_to_nonexistent_scripts_dir(self):
        """曾经有三处写 `scripts/smoke_driver.py`，实际脚本在项目根。"""
        offenders = []
        for name in DOCS:
            for lineno, line in enumerate(read(name).splitlines(), 1):
                if re.search(r"scripts/\S*smoke_driver", line):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "文档引用了不存在的 scripts/ 路径：\n" + "\n".join(offenders))


class DocumentedSymbolTests(ElenvindTestCase):
    #: 文档里绝对不该再出现的幽灵符号（都曾经被引用但从未存在）
    GHOSTS = ("safe_css_url", "MAX_BODY_SIZE", "EVMD", "json_response",
              "SESSION_DAYS")

    def test_no_ghost_symbols_in_docs(self):
        offenders = []
        for name in DOCS:
            for lineno, line in enumerate(read(name).splitlines(), 1):
                for ghost in self.GHOSTS:
                    if re.search(rf"\b{re.escape(ghost)}\b", line):
                        offenders.append(f"{name}:{lineno}: {ghost} -> {line.strip()[:80]}")
        self.assertEqual(offenders, [],
                         "文档引用了不存在的符号：\n" + "\n".join(offenders))

    def test_ghosts_really_do_not_exist(self):
        """反向确认：这些名字在代码里确实没有定义（否则上一条的期望要改）。"""
        code = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (ROOT / "elenvind").rglob("*.py")
            if "__pycache__" not in path.parts)
        for ghost in ("safe_css_url", "MAX_BODY_SIZE", "SESSION_DAYS"):
            with self.subTest(symbol=ghost):
                self.assertNotRegex(code, rf"^\s*(def|class)\s+{ghost}\b", re.M)
                self.assertNotRegex(code, rf"^{ghost}\s*=", re.M)


class StaticAssetDocsTests(ElenvindTestCase):
    def test_documented_icon_and_logo_exist_on_disk(self):
        """文档承诺的缺省图标/logo 必须真的在 elenvind/static/imgs/ 里。

        `DEFAULT_ICON_CANDIDATES` 是 **URL 路径**（如 `/imgs/favicon.ico`），
        不是文件系统路径，因此要拼到 `STATIC_DIR` 上再判存在。
        """
        from elenvind.core.assets import DEFAULT_ICON_CANDIDATES, STATIC_DIR

        for url_path in DEFAULT_ICON_CANDIDATES:
            with self.subTest(candidate=url_path):
                self.assertTrue((STATIC_DIR / url_path.lstrip("/")).is_file(),
                                f"缺省图标候选不存在：{url_path}")
        self.assertTrue((STATIC_DIR / "imgs" / "logo.png").is_file())

    def test_deployment_doc_lists_the_shipped_icons(self):
        text = read("docs/DEPLOYMENT.md")
        for icon in ("favicon.ico", "favicon.png", "logo.png", "github.svg"):
            with self.subTest(icon=icon):
                self.assertIn(icon, text)
        # 不得再声称"仓库不附带图片资产"
        self.assertNotIn("仓库不附带图片资产", text)


class ConfigParityTests(ElenvindTestCase):
    """config.toml 与 config.example.toml 不得"结构性"缺键。

    取值不同是正常的（config.toml 是站长的真实站点，example 是通用模板），
    但**键的有无**不该无故有差异 —— 那通常意味着新加的配置项忘了写进模板。

    例外：`[security.csp]` 下的指令键。`config.example.toml` 把它们写成
    **注释形式的"默认值示意"**（不写就是代码默认），而 `config.toml` 可能
    显式写出其中几条。因此只看"非 csp 的键"，并且额外要求：凡是 config.toml
    显式写出的 csp 指令，其取值必须等于代码默认值（否则就是悄悄放开了 CSP）。
    """

    #: 允许只存在于某一份里的键（前缀匹配）
    ALLOWED_ONLY_IN_REAL_PREFIXES = ("security.csp.",)
    ALLOWED_ONLY_IN_EXAMPLE_PREFIXES = ("security.csp.",)

    def _flatten(self, data, prefix=""):
        out = {}
        for key, value in data.items():
            name = f"{prefix}{key}"
            if isinstance(value, dict):
                out.update(self._flatten(value, name + "."))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                out[name] = f"<{len(value)} 项>"
            else:
                out[name] = value
        return out

    @staticmethod
    def _allowed(key, prefixes):
        return any(key.startswith(prefix) for prefix in prefixes)

    def test_key_sets_match_except_declared_exceptions(self):
        real = self._flatten(tomllib.loads(read("config.toml")))
        example = self._flatten(tomllib.loads(read("config.example.toml")))
        only_real = {key for key in set(real) - set(example)
                     if not self._allowed(key, self.ALLOWED_ONLY_IN_REAL_PREFIXES)}
        only_example = {key for key in set(example) - set(real)
                        if not self._allowed(key, self.ALLOWED_ONLY_IN_EXAMPLE_PREFIXES)}
        self.assertEqual(
            (sorted(only_real), sorted(only_example)), ([], []),
            f"config.toml 独有 {sorted(only_real)}；"
            f"config.example.toml 独有 {sorted(only_example)}")

    def test_explicit_csp_directives_equal_code_defaults(self):
        """config.toml 显式写出的 CSP 指令不得比代码默认更宽松。"""
        from elenvind.core.security import DEFAULT_CSP_DIRECTIVES

        configured = (tomllib.loads(read("config.toml"))
                      .get("security", {}).get("csp", {}) or {})
        for name, values in configured.items():
            if name not in DEFAULT_CSP_DIRECTIVES:
                continue
            with self.subTest(directive=name):
                expected = tuple(DEFAULT_CSP_DIRECTIVES[name])
                actual = tuple(values) if isinstance(values, list) else (values,)
                self.assertEqual(
                    set(actual), set(expected),
                    f"config.toml 的 {name} = {actual} 与代码默认 {expected} 不同"
                    "（放宽 CSP 请显式在文档里说明，而不是顺手改配置）")


class MediaSrcQuotingTests(ElenvindTestCase):
    def test_docs_do_not_show_unquoted_self(self):
        """`"self"` 在 CSP 里匹配一个叫 self 的主机 —— 等于没放行。

        文档示例曾经写成 `media-src = "self"`，照抄会得到一条静默失效的指令。
        """
        for name in DOCS:
            for lineno, line in enumerate(read(name).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") and "media-src" not in stripped:
                    continue
                if re.search(r'(media-src|style-src|img-src)\s*=\s*"\s*self\s*"', line):
                    self.fail(f"{name}:{lineno} 用了未加引号的 self：{stripped}")
