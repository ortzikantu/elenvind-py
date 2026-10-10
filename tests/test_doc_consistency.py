"""文档 / 配置 / 文案一致性守卫。

`config.toml` 的注释写着"守卫（tests/test_doc_consistency.py）只允许'收窄'：
拒绝裸 `http:` / `https:` / `*`"，`config.example.toml` 引用 `docs/CONFIGURATION.md`
—— 本文件让这些承诺真的成立：配置校验、CSP 收窄、i18n 键一致、交付件存在、
依赖声明与代码实际 import 一致、模板保持 Zero-JS。
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
import unittest
from pathlib import Path

from tests import support
from tests.support import PROJECT_ROOT

TEMPLATES = PROJECT_ROOT / "elenvind" / "templates"
PACKAGE = PROJECT_ROOT / "elenvind"


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


class ConfigValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def _reject(self, **overrides):
        from elenvind.core.config import ConfigError, validate_config

        candidate = support.test_config(support.WORKSPACE)
        candidate.update(overrides)
        with self.assertRaises(ConfigError, msg=f"{overrides} must be rejected"):
            validate_config(candidate)

    def test_rejects_invalid_values(self):
        for overrides in (
            {"locale": "fr"},
            {"title": "   "},
            {"site_url": "https://example.com/"},          # 结尾斜杠
            {"site_url": "ftp://example.com"},             # 非 http(s)
            {"admin_user_id": 0},
            {"admin_user_id": True},
            {"max_length": 0},
            {"max_comment_depth": 0},
            {"session_absolute_days": 99999},
            {"server": {"host": "127.0.0.1", "port": 70000, "workers": 1}},
            {"logging": {"level": "verbose"}},
            {"logging": {"level": "info", "rotate": "yes"}},
            {"static": {"css": "javascript:alert(1)"}},
            {"static": {"hero": "https://x/a.png'); background:url(//evil.test)"}},
            {"static": {"hero": "//evil.test/a.png"}},     # 协议相对
            {"params": {"nav": [{"name": "x", "url": "javascript:alert(1)"}]}},
            {"params": {"social": [{"name": "x", "url": "data:text/html,x"}]}},
            {"security": {"csp": {"script-src": ["*"]}}},          # 裸通配
            {"security": {"csp": {"img-src": ["'self'", "http:"]}}},   # 裸协议
            {"security": {"csp": {"bad name": ["'self'"]}}},
            {"security": {"permissions_policy": "a=(); \r\nX-Evil: 1"}},
        ):
            self._reject(**overrides)

    def test_accepts_narrowing_the_csp(self):
        from elenvind.core.config import validate_config

        candidate = support.test_config(support.WORKSPACE)
        candidate["security"] = dict(candidate["security"])
        candidate["security"]["csp"] = {
            "img-src": ["'self'", "data:", "https://cdn.example.com"],
            "media-src": ["'self'"],
            "object-src": False,
        }
        validate_config(candidate)

    def test_mailto_is_allowed_for_social_links_only(self):
        from elenvind.core.config import validate_config

        candidate = support.test_config(support.WORKSPACE)
        validate_config(candidate)                          # params.social 里的 mailto:
        self._reject(static={"favicon": "mailto:me@example.com"})

    def test_shipped_configs_are_valid(self):
        """仓库自带的 config.toml 与 config.example.toml 都必须能启动。"""
        from elenvind.core.config import validate_config

        for name in ("config.toml", "config.example.toml"):
            validate_config(_load_toml(PROJECT_ROOT / name))


class I18nConsistency(unittest.TestCase):
    def test_all_languages_define_the_same_keys(self):
        tables = {lang: _load_toml(PROJECT_ROOT / "i18n" / f"{lang}.toml")
                  for lang in ("en", "zh", "ja")}
        keys = {lang: set(table) for lang, table in tables.items()}
        self.assertEqual(keys["en"], keys["zh"], "zh and en key sets differ")
        self.assertEqual(keys["en"], keys["ja"], "ja and en key sets differ")

    def test_every_key_used_in_code_or_templates_exists(self):
        declared = set(_load_toml(PROJECT_ROOT / "i18n" / "en.toml"))
        used: set[str] = set()

        # .py：用 AST 只看真正的 t(...) 调用（docstring 里的 `t("key")` 示例不算）
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "t"):
                    used |= {arg.value for arg in node.args
                             if isinstance(arg, ast.Constant) and isinstance(arg.value, str)}

        # .html：模板里只有 `t('key')` 这一种形态
        template_pattern = re.compile(r"""\bt\(\s*['"]([a-z0-9_]+)['"]""")
        for path in TEMPLATES.rglob("*.html"):
            used |= set(template_pattern.findall(path.read_text(encoding="utf-8")))

        missing = sorted(used - declared)
        self.assertEqual(missing, [], f"i18n keys referenced but not defined: {missing}")


class Deliverables(unittest.TestCase):
    def test_readme_and_configuration_doc_exist(self):
        self.assertTrue((PROJECT_ROOT / "README.md").is_file())
        self.assertTrue((PROJECT_ROOT / "docs" / "CONFIGURATION.md").is_file())

    def test_deployment_examples_exist(self):
        for name in ("nginx.conf.example", "elenvind.service", "logrotate.conf"):
            self.assertTrue((PROJECT_ROOT / "deploy" / name).is_file(), name)

    def test_requirements_cover_third_party_imports(self):
        names: set[str] = set()
        for path in [*PACKAGE.rglob("*.py"), PROJECT_ROOT / "run.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names.add(node.module.split(".")[0])
        third_party = {name for name in names
                       if name not in sys.stdlib_module_names
                       and name not in {"elenvind", "__future__"}}
        self.assertEqual(third_party, {"gunicorn", "jinja2", "markdown", "markupsafe"},
                         "the dependency set changed; update requirements.txt and this test")

        declared = {line.split("==")[0].strip().lower()
                    for line in (PROJECT_ROOT / "requirements.txt").read_text(
                        encoding="utf-8").splitlines() if line.strip()}
        self.assertTrue({"gunicorn", "jinja2", "markdown", "markupsafe"} <= declared,
                        f"requirements.txt is missing packages: {declared}")

    def test_templates_stay_zero_javascript(self):
        """CSP 声明 script-src 'none'：模板里就不该出现脚本或 javascript: URL。"""
        for path in TEMPLATES.rglob("*.html"):
            source = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("<script", source, f"{path.name} contains a script tag")
            self.assertNotIn("javascript:", source, f"{path.name} contains javascript:")

    def test_no_test_referenced_by_comments_is_missing(self):
        """注释里点名的守卫测试必须真的存在（否则又是"文档说谎"）。"""
        referenced: set[str] = set()
        sources = [*PACKAGE.rglob("*.py"), PROJECT_ROOT / "config.toml",
                   PROJECT_ROOT / "config.example.toml"]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            referenced |= set(re.findall(r"tests/([a-z_]+\.py)", text))
        missing = sorted(name for name in referenced
                         if not (PROJECT_ROOT / "tests" / name).is_file())
        self.assertEqual(missing, [], f"comments reference missing test files: {missing}")


if __name__ == "__main__":
    unittest.main()
