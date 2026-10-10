"""样式漂移守卫：模板 / 代码里用到的 class 必须在内置样式表里有定义。

`style.css` 顶部写着"由 tests/test_styles.py 做'模板用到的类必须被覆盖'的漂移
守卫"—— 本文件就是它。防的是那种"改了模板、忘了样式，页面上多出一块没样式的
内容，而且没有任何报错"的静默劣化。

覆盖两个来源：
- 模板里**静态**写下的 `class="a b c"`；
- Python 里生成的类名（`class="…"` / `"css": "…"`，例如评论树的
  `comment comment-reply`、`is-redacted`）。

含 Jinja 表达式的 `class="{{ row.css }}"` 不算模板静态类（由 Python 侧覆盖），
但同一属性里的字符串字面量（如 `class="form-msg {{ 'success' if … }}"`）会一并检查。
"""
from __future__ import annotations

import re
import unittest

from tests.support import PROJECT_ROOT

PACKAGE = PROJECT_ROOT / "elenvind"
TEMPLATES = PACKAGE / "templates"
STYLESHEET = PACKAGE / "static" / "css" / "style.css"

TEMPLATE_CLASS_RE = re.compile(r'class="([^"]*)"')
CODE_CLASS_RE = re.compile(r'(?:class="|"css":\s*f?")([A-Za-z0-9_ \-]+)"')
CSS_CLASS_RE = re.compile(r"\.([A-Za-z][A-Za-z0-9_-]*)")
JINJA_RE = re.compile(r"[{}]")
LITERAL_RE = re.compile(r"""['"]([a-z][a-z0-9_-]*)['"]""")
JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def template_class_tokens(value: str) -> set[str]:
    """一个 class 属性里的类名集合：静态部分 + Jinja 表达式里的字符串字面量。

    含 Jinja 的属性里，`{{ row.css }}` 这类动态值由 Python 侧负责
    （test_generated_classes_are_styled 覆盖）；但同一属性里的
    `{{ 'success' if … else 'error' }}` 是模板自己决定的类名，必须一起检查。
    """
    tokens = {token for token in JINJA_EXPR_RE.sub(" ", value).split() if token}
    tokens |= set(LITERAL_RE.findall(value))
    return {token for token in tokens if not JINJA_RE.search(token)}


class StyleDrift(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = set(CSS_CLASS_RE.findall(STYLESHEET.read_text(encoding="utf-8")))
        if not cls.css:
            raise AssertionError("built-in stylesheet has no class selectors")

    def _assert_styled(self, used: dict[str, set[str]]) -> None:
        missing = {name: sorted(where) for name, where in used.items()
                   if name not in self.css}
        self.assertEqual(missing, {}, f"classes without any CSS rule: {missing}")

    def test_template_classes_are_styled(self):
        used: dict[str, set[str]] = {}
        for path in TEMPLATES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for match in TEMPLATE_CLASS_RE.finditer(text):
                for token in template_class_tokens(match.group(1)):
                    used.setdefault(token, set()).add(path.name)
        self.assertTrue(used, "no template classes were collected")
        self._assert_styled(used)

    def test_generated_classes_are_styled(self):
        used: dict[str, set[str]] = {}
        for path in PACKAGE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in CODE_CLASS_RE.finditer(text):
                for token in match.group(1).split():
                    used.setdefault(token, set()).add(path.name)
        self.assertTrue(used, "no generated classes were collected")
        self._assert_styled(used)


if __name__ == "__main__":
    unittest.main()
