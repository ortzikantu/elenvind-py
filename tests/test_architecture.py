"""架构守卫：分层依赖方向（Application → Modules → Core）必须是单向的。

为什么单独一个测试文件：`tests/test_core_contract.py` 管的是"Core 承诺的安全能力
不能被业务模块绕过"（请求边界、CSRF、Cookie、SQL 写入约束…）；本文件管的是
**目录与 import 结构**本身。两者互补，互不替代。

判定方式以 **Python import 结构（AST）** 为准，不用目录字符串猜依赖关系。
只有"旧 features 路径残留"这一项才做文本扫描（它就是关于字符串的规则）。

规则与对应测试：

| # | 规则 | 测试 |
|---|---|---|
| 1 | Core 不 import 业务模块，也不 import 装配层 | `test_core_does_not_import_modules_or_the_composition_root` |
| 2 | 模块不 import 装配层（不取全局对象） | `test_modules_do_not_import_the_composition_root` |
| 3 | 模块之间互不 import | `test_modules_do_not_import_each_other` |
| 4 | 无循环 import | `test_module_level_import_graph_is_acyclic`、`test_import_graph_within_modules_is_acyclic`、`test_core_lazy_import_cycles_do_not_grow` |
| 5 | 模块不直接访问 SQLite 写连接（复用 Core Contract 扫描器） | `test_modules_do_not_touch_the_database_directly` |
| 6 | 所有写事务进入 `write_tx()`（复用 Core Contract 扫描器） | `test_db_writes_still_go_through_write_tx` |
| 7 | 无旧 `features/` 路径残留 | `test_no_legacy_features_paths` |
| 8 | 模块公开入口形状 + 每个模块必须在清单里声明 | `test_module_public_entrypoints` |
| 9 | `modules/__init__.py` 是纯命名空间（无副作用导入） | `test_modules_package_has_no_side_effect_imports` |
| 10 | 每个模块都能被独立 import（无导入顺序依赖） | `test_every_module_imports_standalone` |
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT

#: 复用 Core Contract 的扫描器：只维护一套"模块里不能出现的数据库用法"，
#: 两个测试文件从不同角度断言同一件事（这里断言"规则存在且被应用"，
#: 那里断言"当前代码遵守"）。
from tests.test_core_contract import (
    _db_module_write_offenders,
    _driver_import_offenders,
    _module_sources,
    _sql_execution_offenders,
)

PACKAGE = PROJECT_ROOT / "elenvind"
CORE = PACKAGE / "core"
MODULES = PACKAGE / "modules"
COMPOSITION_ROOT = (
    PACKAGE / "app.py",        # 组合入口：装配 Core App + 模块
    PACKAGE / "wsgi.py",       # 生产入口：startup(STARTUP_HOOKS) + application
)

#: 每个业务模块的公开入口（缺一个就要显式加到这里 —— 避免"偷偷多一个模块"）。
#: 形状不强制统一：system 是错误页提供者，没有 `register`。
MODULE_ENTRYPOINTS = {
    "blog": ("routes", "register"),
    "pages": ("routes", "register"),
    "auth": ("routes", "register"),
    "users": ("routes", "register"),
    "admin": ("routes", "register"),
    "seo": ("routes", "register"),
    "system": ("routes", "not_found"),
}

#: Core 里**既有的**、靠函数内延迟 import 打破的环（技术债，不是本次引入的）。
#: 棘轮守卫：只允许减少，不允许增加 —— 新代码不得再用延迟 import 掩盖循环依赖。
#: 想缩小这个集合需要重构 Core 的 config/security/templating 依赖关系，
#: 属于独立的一次改动（见最终报告"技术债"）。
KNOWN_CORE_LAZY_CYCLE_EDGES = frozenset({
    ("elenvind.core.config", "elenvind.core.security"),
    ("elenvind.core.context", "elenvind.core.config"),
    ("elenvind.core.context", "elenvind.core.db_user"),
    ("elenvind.core.db_base", "elenvind.core.config"),
    ("elenvind.core.db_session", "elenvind.core.config"),
    ("elenvind.core.db_user", "elenvind.core.utils"),
    ("elenvind.core.http", "elenvind.core.config"),
    ("elenvind.core.http", "elenvind.core.security"),
    ("elenvind.core.http", "elenvind.core.session"),
    ("elenvind.core.security", "elenvind.core.config"),
    ("elenvind.core.security", "elenvind.core.csrf"),
    ("elenvind.core.security", "elenvind.core.templating"),
    ("elenvind.core.session", "elenvind.core.config"),
    ("elenvind.core.templating", "elenvind.core.assets"),
})

#: 旧路径残留的精确形状（只匹配"路径/标识符"，不匹配英文单词 features）。
LEGACY_PATH_PATTERNS = (
    r"elenvind\.features",
    r"elenvind/features",
    r"\.\.features",
    r"features/registry\.py",
    r"docs/development/features\.md",
    r"FEATURES_DIR",
    r"elenvind\.modules\.registry",   # 已删除的旧装配点
)


# ======================= import 图（AST） =======================

def _package_of(path: Path) -> str:
    """文件对应的包名（例如 `elenvind/modules/blog/routes.py` -> `elenvind.modules.blog`）。"""
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

    "module" = 模块级（导入时立即执行，决定 import 期正确性）；
    "lazy"   = 函数/类体内的延迟导入（运行到那里才执行）。
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
                    yield _resolve_relative(path, child), kind


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
    """返回所有环（每个环是节点元组列表，含回到起点的那一步）。"""
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
    """source / target 是否属于 modules 下的**不同**模块。"""
    if ".modules." not in source or ".modules." not in target:
        return False
    own = source.rsplit(".", 1)[0]          # 例如 elenvind.modules.blog
    return not (target == own or target.startswith(own + "."))


# ======================= 1~3. 依赖方向 =======================

class LayerDirectionTests(unittest.TestCase):
    """Core 不认识业务；模块不认识装配层；模块之间互不认识。"""

    def test_core_does_not_import_modules_or_the_composition_root(self):
        offenders = []
        for path in _python_files(CORE):
            for target, kind in _iter_imports(path):
                if target.startswith("elenvind.modules") or \
                        target in ("elenvind.app", "elenvind.wsgi"):
                    offenders.append(f"{path.name}: -> {target} [{kind}]")
        self.assertEqual(offenders, [],
                         "Core 反向依赖了业务模块/装配层：\n" + "\n".join(offenders))

    def test_modules_do_not_import_the_composition_root(self):
        offenders = []
        for path in _python_files(MODULES):
            for target, kind in _iter_imports(path):
                if target in ("elenvind.app", "elenvind.wsgi", "elenvind"):
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

    def test_composition_root_imports_core_and_modules(self):
        """反向确认：装配层确实同时引用 Core 与各模块（否则上面的守卫会形同虚设）。"""
        imported = set()
        for path in COMPOSITION_ROOT:
            imported.update(target for target, _ in _iter_imports(path))
        self.assertIn("elenvind.core.app", imported)
        for name in MODULE_ENTRYPOINTS:
            self.assertTrue(any(target.startswith(f"elenvind.modules.{name}") for target in imported),
                            f"装配层没有引用模块 {name}")


# ======================= 4. 循环依赖 =======================

class ImportCycleTests(unittest.TestCase):
    """不允许循环依赖，也不允许用延迟 import 掩盖新的环。"""

    def test_module_level_import_graph_is_acyclic(self):
        """模块级 import 决定 import 期正确性：必须无环。"""
        cycles = _find_cycles({(s, d) for s, d, k in _package_edges() if k == "module"})
        self.assertEqual(cycles, [],
                         "模块级 import 出现环：\n" + "\n".join(" -> ".join(c) for c in cycles))

    def test_import_graph_within_modules_is_acyclic(self):
        """业务模块层（含延迟 import）必须完全无环。"""
        pairs = {(s, d) for s, d, k in _package_edges()
                 if ".modules." in s or ".modules." in d}
        cycles = _find_cycles(pairs)
        self.assertEqual(cycles, [],
                         "业务模块出现环：\n" + "\n".join(" -> ".join(c) for c in cycles))

    def test_core_lazy_import_cycles_do_not_grow(self):
        """棘轮：Core 既有的"延迟 import 环"只允许减少。

        这些边是历史遗留（配置/安全/模板三个模块互相需要），修它们要动 Core 的
        安全关键路径，属于独立改动 —— 但**新代码不允许再加**：
        新增一条用延迟 import 掩盖的环会让这条测试变红。
        """
        pairs = {(s, d) for s, d, k in _package_edges()}
        cycle_edges = set()
        for cycle in _find_cycles(pairs):
            for index in range(len(cycle) - 1):
                cycle_edges.add((cycle[index], cycle[index + 1]))
        lazy_edges = {(s, d) for s, d, k in _package_edges()
                      if k == "lazy" and ".core." in s and ".core." in d}
        current = cycle_edges & lazy_edges
        new_ones = current - KNOWN_CORE_LAZY_CYCLE_EDGES
        self.assertEqual(sorted(new_ones), [],
                         "新增了靠延迟 import 掩盖的循环依赖：\n"
                         + "\n".join(f"{s} -> {d}" for s, d in sorted(new_ones)))


# ======================= 5~6. 数据库边界（复用 Core Contract 扫描器） =======================

class DatabaseBoundaryStillEnforcedTests(unittest.TestCase):
    """目录改名不得放宽 C0 的静态扫描范围（扫描目标现在叫 modules/）。"""

    def test_scan_target_is_the_modules_tree(self):
        sources = list(_module_sources())
        self.assertGreaterEqual(len(sources), len(MODULE_ENTRYPOINTS),
                                "模块扫描没有覆盖到全部模块")
        for path, _ in sources:
            self.assertTrue(path.is_relative_to(MODULES),
                            f"扫描到了 modules/ 之外的路径：{path}")

    def test_modules_do_not_touch_the_database_directly(self):
        offenders = []
        for path, source in _module_sources():
            for detail in _sql_execution_offenders(source):
                offenders.append(f"{path.name}: {detail}")
            for detail in _driver_import_offenders(source):
                offenders.append(f"{path.name}: {detail}")
        self.assertEqual(offenders, [],
                         "模块直接碰数据库（必须走 Core 的业务 DB API）：\n"
                         + "\n".join(offenders))

    def test_db_writes_still_go_through_write_tx(self):
        offenders = []
        for path in sorted(CORE.glob("db_*.py")):
            offenders.extend(_db_module_write_offenders(
                path.name, path.read_text(encoding="utf-8")))
        self.assertEqual(offenders, [],
                         "这些 DB 函数执行了写 SQL 却没有 write_tx()/conn 边界：\n"
                         + "\n".join(offenders))


# ======================= 7. 旧路径残留 =======================

class LegacyPathTests(unittest.TestCase):
    """仓库里不得再出现旧 `features/` 路径（结构迁移必须彻底）。"""

    #: 守卫文件自己会写下这些模式（作为规则），因此跳过自己。
    SELF = Path(__file__).resolve()

    SCAN = (
        (PACKAGE, (".py",)),
        (PROJECT_ROOT / "tests", (".py",)),
        (PROJECT_ROOT / "docs", (".md", ".example")),
    )
    SCAN_FILES = ("README.md", "config.toml", "config.example.toml",
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

    def test_no_legacy_features_paths(self):
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
                         "仍有旧 features 路径引用：\n" + "\n".join(offenders))

    def test_documentation_uses_the_new_name(self):
        guide = PROJECT_ROOT / "docs" / "development" / "modules.md"
        self.assertTrue(guide.is_file(), "开发指南应改名为 docs/development/modules.md")
        self.assertFalse((PROJECT_ROOT / "docs" / "development" / "features.md").exists(),
                         "旧文档路径不该继续存在")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/development/modules.md", readme)


# ======================= 8~10. 模块公开入口与独立性 =======================

class ModuleShapeTests(unittest.TestCase):
    """模块的公开入口形状 + 独立可导入性。"""

    def _module_names(self):
        return sorted(p.name for p in MODULES.iterdir()
                      if p.is_dir() and (p / "__init__.py").is_file())

    def test_every_module_is_declared_with_an_entrypoint(self):
        names = self._module_names()
        self.assertEqual(sorted(MODULE_ENTRYPOINTS), names,
                         "模块清单与磁盘不一致（新增/删除模块必须显式登记）")

    def test_module_public_entrypoints_are_callable(self):
        import importlib

        for name, (module_attr, function_name) in MODULE_ENTRYPOINTS.items():
            with self.subTest(module=name):
                module = importlib.import_module(
                    f"elenvind.modules.{name}.{module_attr}")
                entry = getattr(module, function_name, None)
                self.assertTrue(callable(entry),
                                f"modules/{name}/{module_attr}.py 缺少可调用的 {function_name}()")

    def test_modules_package_has_no_side_effect_imports(self):
        """`elenvind.modules` 是纯命名空间：import 它不该触发任何业务模块。"""
        tree = ast.parse((MODULES / "__init__.py").read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [],
                         "modules/__init__.py 不该 import 任何东西（副作用/循环风险）")

    def test_every_module_imports_standalone(self):
        """每个模块都必须能单独 import（没有隐含的导入顺序要求）。"""
        for name in MODULE_ENTRYPOINTS:
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-c", f"import elenvind.modules.{name}"],
                    cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0,
                                 f"import elenvind.modules.{name} 失败：\n{result.stderr[-800:]}")


if __name__ == "__main__":
    unittest.main()
