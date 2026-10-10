"""架构守卫：依赖方向与"单一实现点"。

`elenvind/app.py` 的注释写着"依赖方向由 tests/test_architecture.py 守卫"，
`core/templating.py` 说"不得自建 Jinja Environment（由 tests/test_core_contract.py
静态守卫）"—— 这些守卫以前并不存在（仓库里没有 tests/）。本文件把它们变成真的：
纯 AST / 文本扫描，不需要启动应用。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "elenvind"


def python_files() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py")
                  if "__pycache__" not in path.parts)


def imported_modules(path: Path) -> set[str]:
    """该文件 import 的**绝对**模块名（相对导入按所在包解析）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(ROOT).with_suffix("").parts[:-1])
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - (node.level - 1)] if node.level else []
            if node.module:
                base = base + node.module.split(".")
            if base:
                modules.add(".".join(base))
    return modules


def source_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def executed_sql(path: Path) -> list[str]:
    """`…execute("字面量 SQL")` 里真正被执行的语句（注释与 docstring 不算）。"""
    statements: list[str] = []
    for node in ast.walk(ast.parse(source_of(path), filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("execute", "executemany", "executescript"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            statements.append(first.value)
        elif isinstance(first, ast.JoinedStr):          # f-string：拼出静态部分
            statements.append("".join(
                part.value for part in first.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)))
    return statements


def layer_of(path: Path) -> str:
    relative = path.relative_to(PACKAGE)
    return relative.parts[0] if len(relative.parts) > 1 else "(top)"


class DependencyDirection(unittest.TestCase):
    """core ↛ db/modules、db ↛ core/modules、modules ↛ 兄弟模块/装配层。"""

    def test_core_does_not_know_db_or_modules(self):
        for path in python_files():
            if layer_of(path) != "core":
                continue
            for module in imported_modules(path):
                self.assertFalse(
                    module == "elenvind.db" or module.startswith("elenvind.db."),
                    f"{path.name} (core) must not import db: {module}")
                self.assertFalse(
                    module == "elenvind.modules" or module.startswith("elenvind.modules."),
                    f"{path.name} (core) must not import modules: {module}")

    def test_db_does_not_know_core_or_modules(self):
        for path in python_files():
            if layer_of(path) != "db":
                continue
            for module in imported_modules(path):
                self.assertFalse(
                    module == "elenvind.core" or module.startswith("elenvind.core."),
                    f"{path.name} (db) must not import core: {module}")
                self.assertFalse(
                    module == "elenvind.modules" or module.startswith("elenvind.modules."),
                    f"{path.name} (db) must not import modules: {module}")

    def test_modules_do_not_import_sibling_modules(self):
        for path in python_files():
            if layer_of(path) != "modules":
                continue
            own = path.relative_to(PACKAGE).parts[1]          # modules/<own>/...
            for module in imported_modules(path):
                if not module.startswith("elenvind.modules."):
                    continue
                other = module.split(".")[2]
                self.assertEqual(
                    own, other,
                    f"{path} imports sibling module {other!r}; "
                    f"cross-module data must be injected by elenvind/app.py")

    def test_modules_do_not_import_the_composition_root(self):
        for path in python_files():
            if layer_of(path) != "modules":
                continue
            for module in imported_modules(path):
                self.assertNotIn(
                    module, ("elenvind.app", "elenvind.wsgi"),
                    f"{path.name} must not import the composition root: {module}")


class SingleSourceOfTruth(unittest.TestCase):
    """几个"全项目只能有一处"的实现点。"""

    def test_only_db_imports_sqlite3(self):
        for path in python_files():
            if "sqlite3" in imported_modules(path):
                self.assertEqual(
                    layer_of(path), "db",
                    f"{path} imports sqlite3; SQL is only allowed inside elenvind/db/")

    def test_only_db_opens_sqlite_connections(self):
        for path in python_files():
            if layer_of(path) == "db":
                continue
            self.assertNotIn("sqlite3.connect", source_of(path),
                             f"{path.name} opens a SQLite connection directly")

    def test_only_db_writes_sql(self):
        """非 db 层不得执行写 SQL（只看真正的 execute 调用，注释/docstring 不算）。"""
        markers = ("INSERT INTO", "DELETE FROM", "BEGIN IMMEDIATE")
        for path in python_files():
            if layer_of(path) == "db":
                continue
            for statement in executed_sql(path):
                for marker in markers:
                    self.assertFalse(marker in statement.upper(),
                                     f"{path.name} executes {marker!r}; writes belong to db/")

    def test_only_templating_builds_a_jinja_environment(self):
        for path in python_files():
            if path.name == "templating.py" and layer_of(path) == "core":
                continue
            source = source_of(path)
            self.assertNotIn("Environment(", source,
                             f"{path.name} builds its own Jinja Environment")

    def test_only_core_touches_jinja_and_markdown_libraries(self):
        for path in python_files():
            modules = imported_modules(path)
            if "jinja2" in modules or any(m.startswith("jinja2.") for m in modules):
                self.assertEqual(path.name, "templating.py",
                                 f"{path.name} imports jinja2 directly")
            if "markdown" in modules or any(m.startswith("markdown.") for m in modules):
                self.assertEqual(path.name, "markdown.py",
                                 f"{path.name} imports the markdown library directly")

    def test_only_core_security_performs_password_hashing(self):
        """scrypt / pbkdf2 只允许出现在 core.security。

        `hashlib.sha256`（静态资源 ETag）与 `hmac.compare_digest`
        （CSRF 双提交令牌比对）是**非密码派生**用途，不在此列；这里只盯
        密码派生函数本身。
        """
        forbidden = {("hashlib", "scrypt"), ("hashlib", "pbkdf2_hmac")}
        for path in python_files():
            if path.name == "security.py" and layer_of(path) == "core":
                continue
            for node in ast.walk(ast.parse(source_of(path), filename=str(path))):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and (node.value.id, node.attr) in forbidden):
                    self.fail(f"{path.name} calls {node.value.id}.{node.attr}; "
                              f"password primitives live in core.security")

    def test_only_http_and_security_build_set_cookie(self):
        """`b"set-cookie"` 这类字面量只允许出现在 core/security.py 与 core/http.py。"""
        for path in python_files():
            if layer_of(path) != "core" or path.name in ("http.py", "security.py"):
                continue
            for node in ast.walk(ast.parse(source_of(path), filename=str(path))):
                if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
                    self.assertFalse(
                        b"set-cookie" in node.value.lower(),
                        f"{path.name} builds a Set-Cookie header literal")


if __name__ == "__main__":
    unittest.main()
