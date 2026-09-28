"""配置与 i18n 测试：启动校验、路径解析、Cookie 前缀、文案回退与损坏检测。"""
import os
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT, ElenvindTestCase

from elenvind import config as config_module
from elenvind import i18n as i18n_module
from elenvind.config import (
    ConfigError,
    ROOT,
    apply_runtime_config,
    load_config,
    resolve_path,
    validate_config,
)


class PathResolutionTests(unittest.TestCase):
    def test_relative_paths_resolve_against_project_root(self):
        self.assertEqual(resolve_path("sqlite.db", ROOT / "x"), ROOT / "sqlite.db")
        self.assertEqual(resolve_path("logs/app.log", ROOT / "x"), ROOT / "logs" / "app.log")

    def test_blank_and_none_fall_back_to_default(self):
        default = ROOT / "fallback"
        self.assertEqual(resolve_path("", default), default)
        self.assertEqual(resolve_path(None, default), default)
        self.assertEqual(resolve_path("   ", default), default)

    def test_absolute_paths_are_kept(self):
        absolute = ROOT / "somewhere" / "file.db"
        self.assertEqual(resolve_path(str(absolute), ROOT / "x"), absolute)

    def test_path_does_not_depend_on_cwd(self):
        cwd = os.getcwd()
        try:
            os.chdir(ROOT)
            first = resolve_path("articles", ROOT / "articles")
            os.chdir(PROJECT_ROOT / "docs")
            second = resolve_path("articles", ROOT / "articles")
        finally:
            os.chdir(cwd)
        self.assertEqual(first, second)


class ValidateConfigTests(ElenvindTestCase):
    """校验用基类里的合成配置（不需要真实 config.toml）。"""

    def test_valid_config_passes(self):
        validate_config(self._config)

    def test_missing_title(self):
        self._config["title"] = ""
        with self.assertRaises(ConfigError):
            validate_config()

    def test_bad_locale(self):
        self._config["locale"] = "klingon"
        with self.assertRaises(ConfigError):
            validate_config()
        self._config["locale"] = "zh-CN"        # 带区域后缀合法
        validate_config()

    def test_bad_port_and_host(self):
        self._config["server"]["port"] = 0
        with self.assertRaises(ConfigError):
            validate_config()
        self._config["server"]["port"] = 70000
        with self.assertRaises(ConfigError):
            validate_config()
        self._config["server"]["port"] = 6789
        self._config["server"]["host"] = ""
        with self.assertRaises(ConfigError):
            validate_config()

    def test_bad_site_url(self):
        for value in ("example.com", "ftp://example.com", "https://example.com/",
                      "javascript:alert(1)"):
            with self.subTest(value=value):
                self._config["site_url"] = value
                with self.assertRaises(ConfigError):
                    validate_config()
        self._config["site_url"] = ""
        validate_config()
        self._config["site_url"] = "https://example.com"
        validate_config()

    def test_bad_admin_user_id(self):
        for value in (0, -1, "1", 1.5, True):
            with self.subTest(value=value):
                self._config["admin_user_id"] = value
                with self.assertRaises(ConfigError):
                    validate_config()
        self._config["admin_user_id"] = None
        validate_config()

    def test_bad_limits(self):
        self._config["max_length"] = 0
        with self.assertRaises(ConfigError):
            validate_config()
        self._config["max_length"] = 1000
        self._config["comment_limits"] = {"max_per_user": 0}
        with self.assertRaises(ConfigError):
            validate_config()

    def test_bad_logging(self):
        self._config["logging"]["level"] = "verbose"
        with self.assertRaises(ConfigError):
            validate_config()

    def test_bad_static_urls(self):
        for key in ("css", "favicon", "logo", "hero"):
            with self.subTest(key=key):
                self._config["static"] = {key: "javascript:alert(1)"}
                with self.assertRaises(ConfigError):
                    validate_config()
                self._config["static"] = {key: "//evil.example.com/x.png"}
                with self.assertRaises(ConfigError):
                    validate_config()
        self._config["static"] = {"css": "/style.css", "hero": "https://e.com/h.webp"}
        validate_config()

    def test_registration_enabled_must_be_bool(self):
        self._config["registration_enabled"] = "yes"
        with self.assertRaises(ConfigError):
            validate_config()

    def test_missing_secret_key_fails_startup(self):
        original = os.environ.pop("SECRET_KEY", None)
        try:
            with self.assertRaises(ConfigError):
                validate_config()
        finally:
            if original is not None:
                os.environ["SECRET_KEY"] = original

    def test_articles_dir_must_be_directory_when_present(self):
        file_path = self.tmpdir / "not-a-dir"
        file_path.write_text("x", encoding="utf-8")
        self._config["articles_dir"] = str(file_path)
        with self.assertRaises(ConfigError):
            validate_config()


class CookiePrefixConfigTests(ElenvindTestCase):
    def test_prefix_disabled_by_default(self):
        from elenvind.security import cookie_name
        apply_runtime_config()
        self.assertEqual(cookie_name("session"), "session")

    def test_prefix_enabled_via_config(self):
        from elenvind import security
        try:
            self._config["server"]["cookie_prefix"] = True
            apply_runtime_config()
            self.assertEqual(security.cookie_name("session"), "__Host-session")
        finally:
            self._config["server"]["cookie_prefix"] = False
            apply_runtime_config()


class RealConfigFileTests(unittest.TestCase):
    """真实 config.toml 必须自身合法（防止交付一个起不来的仓库）。"""

    def test_repository_config_is_valid(self):
        """仓库里的 config.toml 必须自身合法（若存在的话）。

        该文件可能由部署方替换/由外部工具同步，因此缺失或含自定义取值时
        只跳过而不判失败；真正强制校验的是 config.example.toml。
        """
        if not (PROJECT_ROOT / "config.toml").exists():
            self.skipTest("config.toml not present in this checkout")
        original = dict(config_module.config)
        try:
            load_config()
            validate_config()
        finally:
            config_module.config.clear()
            config_module.config.update(original)

    def test_example_config_is_valid_and_sanitized(self):
        import tomllib

        example = PROJECT_ROOT / "config.example.toml"
        self.assertTrue(example.exists(), "config.example.toml is missing")
        with open(example, "rb") as handle:
            data = tomllib.load(handle)
        self.assertNotIn("192.168.", example.read_text(encoding="utf-8"))
        self.assertIn("site_url", data)
        self.assertIn("admin_user_id", data)

        original = dict(config_module.config)
        try:
            config_module.config.clear()
            config_module.config.update(data)
            validate_config()
        finally:
            config_module.config.clear()
            config_module.config.update(original)

    def test_default_config_keys_match_docs_expectations(self):
        import tomllib

        with open(PROJECT_ROOT / "config.example.toml", "rb") as handle:
            data = tomllib.load(handle)
        for key in ("locale", "title", "site_url", "admin_user_id", "max_length",
                    "max_comment_depth", "registration_enabled", "max_body_size",
                    "database", "articles_dir", "usrpages_dir",
                    "comment_limits", "login_limits", "register_limits",
                    "server", "pagination", "static", "params", "logging"):
            self.assertIn(key, data, f"config.example.toml is missing {key}")


class I18nTests(unittest.TestCase):
    def setUp(self):
        self._original = i18n_module._TABLES
        i18n_module.load()

    def tearDown(self):
        i18n_module._TABLES = self._original

    def test_all_locales_have_identical_keys(self):
        tables = i18n_module._TABLES
        base = set(tables["en"])
        self.assertTrue(base)
        for lang in ("zh", "ja"):
            self.assertEqual(set(tables[lang]) - base, set(), f"{lang} has extra keys")
            self.assertEqual(base - set(tables[lang]), set(), f"{lang} is missing keys")

    def test_lookup_and_fallback(self):
        self.assertTrue(i18n_module.t("en", "auth_login_title"))
        self.assertTrue(i18n_module.t("zh", "auth_login_title"))
        # 目标语言缺词时回退英文
        tables = i18n_module._TABLES
        tables["zh"].pop("auth_login_title", None)
        self.assertEqual(i18n_module.t("zh", "auth_login_title"),
                         tables["en"]["auth_login_title"])
        # 全都没有时返回键名，不抛异常
        self.assertEqual(i18n_module.t("zh", "no_such_key_at_all"), "no_such_key_at_all")

    def test_formatting_placeholders(self):
        self.assertIn("2026", i18n_module.t("en", "footer_rights", year=2026, name="X"))
        # 缺参数时原样返回，不 500
        self.assertIsInstance(i18n_module.t("en", "footer_rights", year=2026), str)

    def test_normalize(self):
        cases = {"en": "en", "EN": "en", "zh-CN": "zh", "zh_TW": "zh", "ja-JP": "ja",
                 "fr": "en", "": "en", None: "en"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(i18n_module.normalize(value), expected)

    def test_import_does_not_read_files(self):
        source = (PROJECT_ROOT / "elenvind" / "i18n.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]
        self.assertNotIn("\nload()", body)

    def test_malformed_toml_raises_at_load(self):
        original_dir = i18n_module._I18N_DIR
        broken = Path(self._tmpdir())
        (broken / "en.toml").write_text('key = "unterminated\n', encoding="utf-8")
        i18n_module._I18N_DIR = broken
        try:
            with self.assertRaises(i18n_module.I18nError):
                i18n_module.load()
        finally:
            i18n_module._I18N_DIR = original_dir

    def test_non_string_value_raises_at_load(self):
        original_dir = i18n_module._I18N_DIR
        broken = Path(self._tmpdir())
        (broken / "en.toml").write_text("key = 42\n", encoding="utf-8")
        i18n_module._I18N_DIR = broken
        try:
            with self.assertRaises(i18n_module.I18nError):
                i18n_module.load()
        finally:
            i18n_module._I18N_DIR = original_dir

    def test_missing_key_never_raises(self):
        self.assertEqual(i18n_module.t("en", None), None)

    def _tmpdir(self):
        root = PROJECT_ROOT / ".testtmp"
        root.mkdir(exist_ok=True)
        path = root / f"i18n-{os.getpid()}-{id(self)}"
        path.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path


if __name__ == "__main__":
    unittest.main()
