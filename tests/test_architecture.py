"""架构守卫：分层、SQLite 边界与模块边界。

目标架构（见 docs/ARCHITECTURE.md）：

    Application（elenvind/app.py、elenvind/wsgi.py）
        │
        ├──► core     项目专属运行时基座（不认识 db，也不认识 modules）
        ├──► db       持久化基座：**唯一** SQLite 边界（不认识 core/modules/app）
        └──► modules  业务模块（可依赖 core 与 db；模块之间零 import）

判定方式：以 Python **import 结构（AST）** 为准，不用目录字符串猜依赖关系；
只有"旧结构路径残留"这一项才做文本扫描（它本身就是关于字符串的规则）。

| # | 规则 | 测试 |
|---|---|---|
| 1 | core ↛ db / core ↛ modules / core ↛ app | `test_core_only_depends_on_itself` |
| 2 | db ↛ core / db ↛ modules / db ↛ app | `test_db_is_independent` |
| 3 | modules ↛ app | `test_modules_do_not_import_the_composition_root` |
| 4 | modules 之间零 import | `test_modules_do_not_import_each_other` |
| 5 | 只有 db/ 可以 import sqlite3 | `test_only_db_imports_sqlite3` |
| 6 | 只有 db/ 可以执行 SQL / 提交事务 | `test_sql_and_transactions_stay_inside_db` |
| 7 | `BEGIN IMMEDIATE` 只出现在 db/transaction.py | `test_begin_immediate_only_in_the_transaction_module` |
| 8 | 模块不得直接访问 SQLite（复用 Core Contract 扫描器） | `test_modules_do_not_touch_the_database_directly` |
| 9 | 所有写 SQL 都在 write_tx()/事务连接内 | `test_db_writes_still_go_through_write_tx` |
| 10 | 无 import 环（模块级；db 层彻底无环；Core 延迟环有棘轮） | `ImportCycleTests` |
| 11 | 无旧 features/ 与 core/db_* 残留；文档指向新结构 | `LegacyPathTests` |
| 12 | 模块公开入口形状 + db 公开面稳定 | `ModuleShapeTests` |
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT

#: 复用 Core Contract 的扫描器：只维护一套"模块里不能出现的数据库用法"。
from tests.test_core_contract import (
    _db_module_write_offenders,
    _driver_import_offenders,
    _module_sources,
    _sql_execution_offenders,
)

PACKAGE = PROJECT_ROOT / "elenvind"
CORE = PACKAGE / "core"
DB = PACKAGE / "db"
MODULES = PACKAGE / "modules"
COMPOSITION_ROOT = (
    PACKAGE / "app.py",        # 组合入口：装配 Core App + db + 模块
    PACKAGE / "wsgi.py",       # 生产入口：startup(...) + application
)

#: 每个业务模块的公开入口（缺一个就要显式加到这里 —— 避免"偷偷多一个模块"）。
MODULE_ENTRYPOINTS = {
    "blog": ("routes", "register"),
    "pages": ("routes", "register"),
    "auth": ("routes", "register"),
    "users": ("routes", "register"),
    "admin": ("routes", "register"),
    "seo": ("routes", "register"),
    "system": ("routes", "not_found"),   # 错误页提供者，没有 register
}

#: Core 里**既有的**、靠函数内延迟 import 打破的环（技术债，非本次引入）。
#: 棘轮守卫：只允许减少，不允许增加（缩小它需要一次独立的 Core 内部重构）。
KNOWN_CORE_LAZY_CYCLE_EDGES = frozenset({
    ("elenvind.core.config", "elenvind.core.security"),
    ("elenvind.core.context", "elenvind.core.config"),
    ("elenvind.core.csrf", "elenvind.core.utils"),
    ("elenvind.core.http", "elenvind.core.config"),
    ("elenvind.core.http", "elenvind.core.security"),
    ("elenvind.core.http", "elenvind.core.session"),
    ("elenvind.core.security", "elenvind.core.config"),
    ("elenvind.core.security", "elenvind.core.csrf"),
    ("elenvind.core.security", "elenvind.core.templating"),
    ("elenvind.core.session", "elenvind.core.config"),
    ("elenvind.core.templating", "elenvind.core.assets"),
})

#: 旧结构残留的精确形状（只匹配"路径/标识符"，不匹配英文单词 features）。
LEGACY_PATH_PATTERNS = (
    r"elenvind\.features",
    r"elenvind/features",
    r"\.\.features",
    r"features/registry\.py",
    r"docs/development/features\.md",
    r"FEATURES_DIR",
    r"elenvind\.core\.db_",          # 迁移前 SQLite 实现曾住在 core/
    r"elenvind\.modules\.registry",
)


# ======================= import 图（AST） =======================

def _package_of(path: Path) -> str:
    return ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts[:-1])


def _module_of(path: Path) -> str:
    return ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)


def _resolve_relative(path: Path, node: ast.ImportFrom) -> str:
    base = _package_of(path)
    for _ in range(node.level - 1):
        base = base.rpartition(".")[0]
    return ".".join(part for part in (base, node.module or "") if part)


def _iter_imports(path: Path):
    """产出 (目标模块名, "module"|"lazy")。

    "module" = 模块级（导入时立即执行）；"lazy" = 函数/类体内的延迟导入。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        kind = "lazy" if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "module"
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    yield alias.name, kind
            elif isinstance(child, ast.ImportFrom):
                if child.level or (child.module or "").startswith("elenvind"):
                    base = _resolve_relative(path, child)
                    yield base, kind
                    # `from ... import db` 这类写法要展开成 `elenvind.db`，
                    # 否则模块导入包的子模块会被误判成"依赖包根"。
                    for alias in child.names:
                        if alias.name != "*":
                            yield f"{base}.{alias.name}", kind


def _python_files(root: Path):
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _package_edges():
    """包内（elenvind.*）的所有 import 边：[(src_module, dst_module, kind)]。"""
    known = {_module_of(p) for p in _python_files(PACKAGE)}
    edges = []
    for path in _python_files(PACKAGE):
        source = _module_of(path)
        for target, kind in _iter_imports(path):
            if target.startswith("elenvind") and target in known:
                edges.append((source, target, kind))
    return edges


def _find_cycles(pairs):
    adjacency = {}
    for source, target in pairs:
        if source != target:
            adjacency.setdefault(source, set()).add(target)
    state, stack, found = {}, [], []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for nxt in sorted(adjacency.get(node, ())):
            if state.get(nxt, 0) == 1:
                found.append(tuple(stack[stack.index(nxt):] + [nxt]))
            elif state.get(nxt, 0) == 0:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for node in sorted(adjacency):
        if state.get(node, 0) == 0:
            visit(node)
    return found


def _is_cross_module(source: str, target: str) -> bool:
    if ".modules." not in source or ".modules." not in target:
        return False
    own = source.rsplit(".", 1)[0]
    return not (target == own or target.startswith(own + "."))


# ======================= 1~4. 依赖方向 =======================

class LayerDirectionTests(unittest.TestCase):
    """core 不认识 db/modules；db 谁都不认识；模块之间互不认识。"""

    def test_core_only_depends_on_itself(self):
        offenders = []
        for path in _python_files(CORE):
            for target, kind in _iter_imports(path):
                if target.startswith(("elenvind.db", "elenvind.modules")) or \
                        target in ("elenvind.app", "elenvind.wsgi"):
                    offenders.append(f"{path.name}: -> {target} [{kind}]")
        self.assertEqual(offenders, [],
                         "core 反向依赖了 db / 业务模块 / 装配层：\n" + "\n".join(offenders))

    def test_db_is_independent(self):
        """db 只依赖标准库：不认识 core、modules、装配层（配置由装配层注入）。"""
        offenders = []
        for path in _python_files(DB):
            for target, kind in _iter_imports(path):
                if target.startswith(("elenvind.core", "elenvind.modules")) or \
                        target in ("elenvind.app", "elenvind.wsgi"):
                    offenders.append(f"{path.name}: -> {target} [{kind}]")
        self.assertEqual(offenders, [],
                         "db 反向依赖了 core / 业务模块 / 装配层：\n" + "\n".join(offenders))

    def test_modules_do_not_import_the_composition_root(self):
        offenders = []
        for path in _python_files(MODULES):
            for target, kind in _iter_imports(path):
                if target in ("elenvind.app", "elenvind.wsgi"):
                    offenders.append(f"{path.name}: -> {target} [{kind}]")
        self.assertEqual(offenders, [],
                         "业务模块 import 了装配层（会拿到装配期的全局对象）：\n"
                         + "\n".join(offenders))

    def test_modules_do_not_import_each_other(self):
        """模块之间零 import：协作由装配层显式注入（见 elenvind/app.py）。"""
        offenders = []
        for source, target, kind in _package_edges():
            if _is_cross_module(source, target):
                offenders.append(f"{source} -> {target} [{kind}]")
        self.assertEqual(offenders, [],
                         "业务模块之间出现了直接依赖（请改成由装配层注入）：\n"
                         + "\n".join(sorted(offenders)))

    def test_composition_root_imports_core_db_and_modules(self):
        """反向确认：装配层确实同时引用 core、db 与各模块。"""
        imported = set()
        for path in COMPOSITION_ROOT:
            imported.update(target for target, _ in _iter_imports(path))
        self.assertIn("elenvind.core.app", imported)
        self.assertIn("elenvind.db", imported)
        for name in MODULE_ENTRYPOINTS:
            self.assertTrue(any(target.startswith(f"elenvind.modules.{name}")
                                for target in imported),
                            f"装配层没有引用模块 {name}")


# ======================= 5~9. SQLite 边界 =======================

class SqliteBoundaryTests(unittest.TestCase):
    """SQLite 只能出现在 db/：import sqlite3、执行 SQL、提交事务都不例外。"""

    SQL_METHODS = {"execute", "executemany", "executescript"}
    TX_METHODS = {"commit", "rollback"}

    def test_only_db_imports_sqlite3(self):
        offenders = []
        for path in _python_files(PACKAGE):
            if path.is_relative_to(DB):
                continue
            for target, _kind in _iter_imports(path):
                if target == "sqlite3":
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [],
                         "只有 db/ 可以 import sqlite3：\n" + "\n".join(offenders))

    def test_sql_and_transactions_stay_inside_db(self):
        offenders = []
        for path in _python_files(PACKAGE):
            if path.is_relative_to(DB):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in self.SQL_METHODS or node.func.attr in self.TX_METHODS:
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: .{node.func.attr}()")
        self.assertEqual(offenders, [],
                         "db/ 之外不允许执行 SQL 或提交/回滚事务：\n" + "\n".join(offenders))

    def test_begin_immediate_only_in_the_transaction_module(self):
        """`BEGIN IMMEDIATE` 只允许作为**可执行的** SQL 出现在 db/transaction.py。

        按 AST 找"调用 `.execute()` 且首个参数是含该串的字符串字面量"，
        因此 docstring / 注释里提到它的地方不会被误判（历史上正是这种误报）。
        """
        offenders = []
        for path in _python_files(PACKAGE):
            if path.name == "transaction.py" and path.is_relative_to(DB):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if getattr(node.func, "attr", "") != "execute":
                    continue
                literal = node.args[0]
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str) \
                        and "BEGIN IMMEDIATE" in literal.value:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "BEGIN IMMEDIATE 只允许出现在 db/transaction.py：\n"
                         + "\n".join(offenders))

    def test_modules_do_not_touch_the_database_directly(self):
        offenders = []
        for path, source in _module_sources():
            for detail in _sql_execution_offenders(source):
                offenders.append(f"{path.name}: {detail}")
            for detail in _driver_import_offenders(source):
                offenders.append(f"{path.name}: {detail}")
        self.assertEqual(offenders, [],
                         "模块直接碰数据库（必须走 db 层提供的 API）：\n"
                         + "\n".join(offenders))

    def test_db_writes_still_go_through_write_tx(self):
        offenders = []
        for path in sorted(DB.glob("*.py")):
            offenders.extend(_db_module_write_offenders(
                path.name, path.read_text(encoding="utf-8")))
        self.assertEqual(offenders, [],
                         "这些 db 函数执行了写 SQL 却没有 write_tx()/conn 边界：\n"
                         + "\n".join(offenders))

    def test_scan_target_is_the_modules_tree(self):
        sources = list(_module_sources())
        self.assertGreaterEqual(len(sources), len(MODULE_ENTRYPOINTS),
                                "模块扫描没有覆盖到全部模块")
        for path, _ in sources:
            self.assertTrue(path.is_relative_to(MODULES),
                            f"扫描到了 modules/ 之外的路径：{path}")


# ======================= 10. 循环依赖 =======================

class ImportCycleTests(unittest.TestCase):
    """不允许循环依赖，也不允许用延迟 import 掩盖新的环。"""

    def test_module_level_import_graph_is_acyclic(self):
        cycles = _find_cycles({(s, d) for s, d, k in _package_edges() if k == "module"})
        self.assertEqual(cycles, [],
                         "模块级 import 出现环：\n" + "\n".join(" -> ".join(c) for c in cycles))

    def test_db_layer_is_fully_acyclic(self):
        pairs = {(s, d) for s, d, k in _package_edges()
                 if ".db." in s or s == "elenvind.db" or ".db." in d or d == "elenvind.db"}
        cycles = _find_cycles(pairs)
        self.assertEqual(cycles, [],
                         "db 层出现环：\n" + "\n".join(" -> ".join(c) for c in cycles))

    def test_import_graph_within_modules_is_acyclic(self):
        pairs = {(s, d) for s, d, k in _package_edges()
                 if ".modules." in s or ".modules." in d}
        cycles = _find_cycles(pairs)
        self.assertEqual(cycles, [],
                         "业务模块出现环：\n" + "\n".join(" -> ".join(c) for c in cycles))

    def test_core_lazy_import_cycles_do_not_grow(self):
        """棘轮：Core 既有的"延迟 import 环"只允许减少。"""
        cycle_edges = set()
        for cycle in _find_cycles({(s, d) for s, d, _ in _package_edges()}):
            for index in range(len(cycle) - 1):
                cycle_edges.add((cycle[index], cycle[index + 1]))
        lazy_edges = {(s, d) for s, d, k in _package_edges()
                      if k == "lazy" and ".core." in s and ".core." in d}
        new_ones = (cycle_edges & lazy_edges) - KNOWN_CORE_LAZY_CYCLE_EDGES
        self.assertEqual(sorted(new_ones), [],
                         "新增了靠延迟 import 掩盖的循环依赖：\n"
                         + "\n".join(f"{s} -> {d}" for s, d in sorted(new_ones)))


# ======================= 11. 旧路径残留 =======================

class LegacyPathTests(unittest.TestCase):
    """仓库里不得再出现旧结构路径（迁移必须彻底）。"""

    SELF = Path(__file__).resolve()

    SCAN = (
        (PACKAGE, (".py",)),
        (PROJECT_ROOT / "tests", (".py",)),
        (PROJECT_ROOT / "docs", (".md", ".example")),
    )
    SCAN_FILES = ("README.md", "CONTRIBUTING.md", "config.toml", "config.example.toml",
                  "run.py", "smoke_driver.py")

    def _scan_targets(self):
        for root, suffixes in self.SCAN:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in suffixes:
                    continue
                if "__pycache__" in path.parts or path.resolve() == self.SELF:
                    continue
                yield path
        for name in self.SCAN_FILES:
            path = PROJECT_ROOT / name
            if path.is_file():
                yield path

    def test_no_legacy_paths(self):
        import re

        patterns = [re.compile(p) for p in LEGACY_PATH_PATTERNS]
        offenders = []
        for path in self._scan_targets():
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern in patterns:
                    if pattern.search(line):
                        offenders.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "仍有旧结构引用（features/ 或 core/db_*）：\n" + "\n".join(offenders))

    def test_documentation_describes_the_new_tree(self):
        guide = PROJECT_ROOT / "docs" / "development" / "modules.md"
        self.assertTrue(guide.is_file())
        self.assertFalse((PROJECT_ROOT / "docs" / "development" / "features.md").exists())
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/development/modules.md", readme)
        architecture = (PROJECT_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("elenvind/db/", architecture, "架构文档必须描述一级 db/ 包")


# ======================= 12. 模块与 db 公开面 =======================

class ModuleShapeTests(unittest.TestCase):
    """模块的公开入口形状 + db 公开面稳定性。"""

    def _module_names(self):
        return sorted(p.name for p in MODULES.iterdir()
                      if p.is_dir() and (p / "__init__.py").is_file())

    def test_every_module_is_declared_with_an_entrypoint(self):
        self.assertEqual(sorted(MODULE_ENTRYPOINTS), self._module_names(),
                         "模块清单与磁盘不一致（新增/删除模块必须显式登记）")

    def test_module_public_entrypoints_are_callable(self):
        import importlib

        for name, (module_attr, function_name) in MODULE_ENTRYPOINTS.items():
            with self.subTest(module=name):
                module = importlib.import_module(f"elenvind.modules.{name}.{module_attr}")
                entry = getattr(module, function_name, None)
                self.assertTrue(callable(entry),
                                f"modules/{name}/{module_attr}.py 缺少可调用的 {function_name}()")

    def test_modules_package_has_no_side_effect_imports(self):
        tree = ast.parse((MODULES / "__init__.py").read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [],
                         "modules/__init__.py 不该 import 任何东西（副作用/循环风险）")

    def test_db_package_exposes_the_documented_api(self):
        """db 的公开面必须稳定：装配层与模块都依赖它。"""
        import elenvind.db as db

        for name in ("configure", "connect", "write_tx", "init_db", "migrate",
                     "lock_path_for", "IntegrityError", "MigrationError", "DB_PATH",
                     "SCHEMA_VERSION"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(db, name), f"db 未导出 {name}")

    def test_db_does_not_handle_http_or_templates(self):
        """db 是持久化基座：不处理 HTTP、Cookie、模板、业务页面流程。"""
        forbidden = {"http", "cookie", "cookies", "templating", "render_template",
                     "markdown"}
        offenders = []
        for path in _python_files(DB):
            for target, _kind in _iter_imports(path):
                if target.rpartition(".")[2] in forbidden:
                    offenders.append(f"{path.name} -> {target}")
        self.assertEqual(offenders, [], "db 里出现了 HTTP/模板依赖：\n" + "\n".join(offenders))

    def test_every_module_imports_standalone(self):
        for name in MODULE_ENTRYPOINTS:
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-c", f"import elenvind.modules.{name}"],
                    cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0,
                                 f"import elenvind.modules.{name} 失败：\n{result.stderr[-800:]}")


if __name__ == "__main__":
    unittest.main()
