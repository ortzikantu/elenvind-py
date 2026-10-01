"""安全原语测试：密码哈希、自描述格式、渐进式 rehash、CSRF 令牌、Cookie 构造。"""
import hashlib
import unittest

from tests.support import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from elenvind.core import security
from elenvind.core.security import (
    CSRF_MAX_AGE,
    SESSION_MAX_AGE,
    clear_session_cookie,
    csrf_cookie_header,
    generate_csrf_token,
    hash_password,
    is_valid_csrf_token,
    password_needs_rehash,
    set_cookie_header,
    theme_cookie_header,
    verify_csrf_token,
    verify_password,
)


class PasswordHashFormatTests(unittest.TestCase):
    def test_hash_is_self_describing(self):
        stored = hash_password("hunter2")
        parts = stored.split("$")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "scrypt")
        self.assertRegex(parts[1], r"^ln=\d+,r=\d+,p=\d+$")
        self.assertRegex(parts[2], r"^[0-9a-f]{32}$")   # 16 字节盐
        self.assertRegex(parts[3], r"^[0-9a-f]{64}$")   # 32 字节摘要

    def test_hash_uses_configured_cost(self):
        stored = hash_password("hunter2")
        self.assertIn(f"ln={security.SCRYPT_N.bit_length() - 1}", stored)
        self.assertIn(f"r={security.SCRYPT_R}", stored)
        self.assertIn(f"p={security.SCRYPT_P}", stored)

    def test_salt_is_random_per_hash(self):
        first = hash_password("same-password")
        second = hash_password("same-password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("same-password", first))
        self.assertTrue(verify_password("same-password", second))

    def test_pbkdf2_cost_is_raised_above_legacy(self):
        # 旧格式固定 10 万次；新写入的 PBKDF2 参数（若启用）必须显著更高
        self.assertGreater(security.PBKDF2_ITERATIONS, security._LEGACY_PBKDF2_ITERATIONS)


class VerifyPasswordTests(unittest.TestCase):
    def setUp(self):
        self.stored = hash_password("correct horse battery staple")

    def test_accepts_correct_password(self):
        self.assertTrue(verify_password("correct horse battery staple", self.stored))

    def test_rejects_wrong_password(self):
        self.assertFalse(verify_password("Correct horse battery staple", self.stored))
        self.assertFalse(verify_password("", self.stored))

    def test_handles_unicode_password(self):
        stored = hash_password("密码🔒üñí")
        self.assertTrue(verify_password("密码🔒üñí", stored))
        self.assertFalse(verify_password("密码🔒üñ", stored))

    def test_legacy_two_field_hash_still_verifies(self):
        """历史格式 salt$digest（PBKDF2-SHA256 10 万次）必须继续可用。"""
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"legacy-pass", salt, 100_000)
        legacy = f"{salt.hex()}${digest.hex()}"
        self.assertTrue(verify_password("legacy-pass", legacy))
        self.assertFalse(verify_password("wrong", legacy))

    def test_pbkdf2_descriptive_hash_verifies(self):
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"pbkdf-pass", salt, 100_000)
        stored = f"pbkdf2_sha256$100000${salt.hex()}${digest.hex()}"
        self.assertTrue(verify_password("pbkdf-pass", stored))

    def test_malformed_hashes_return_false_without_raising(self):
        for bad in (None, "", "not-a-hash", "a$b$c", "a$b$c$d$e",
                    "scrypt$ln=15,r=8,p=1$zz$zz", "scrypt$ln=15,r=8,p=1$$",
                    "scrypt$ln=15,r=8,p=1$00$00", "$$$", 12345, b"bytes",
                    "unknown_algo$1$00ff$00ff"):
            with self.subTest(bad=bad):
                self.assertFalse(verify_password("x", bad))

    def test_absurd_parameters_do_not_exhaust_resources(self):
        """恶意/损坏的 cost 参数必须被边界检查挡住，而不是真的去算。"""
        for params in ("ln=40,r=8,p=1", "ln=-1,r=8,p=1", "ln=15,r=999,p=1",
                       "ln=abc,r=8,p=1", "ln=15,r=8", "x=1,r=8,p=1"):
            with self.subTest(params=params):
                stored = f"scrypt${params}$00ff$00ff"
                self.assertFalse(verify_password("x", stored))

    def test_programming_errors_are_not_swallowed(self):
        """非预期输入类型不应伪装成"密码错误"以外的静默行为。"""
        with self.assertRaises(AttributeError):
            verify_password(None, self.stored)   # None 没有 encode()


class PasswordRehashTests(unittest.TestCase):
    def test_fresh_hash_does_not_need_rehash(self):
        self.assertFalse(password_needs_rehash(hash_password("x")))

    def test_legacy_hash_needs_rehash(self):
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"x", salt, 100_000)
        self.assertTrue(password_needs_rehash(f"{salt.hex()}${digest.hex()}"))

    def test_pbkdf2_hash_needs_rehash(self):
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"x", salt, 600_000)
        stored = f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"
        self.assertTrue(password_needs_rehash(stored))

    def test_weaker_scrypt_params_need_rehash(self):
        stored = f"scrypt$ln=14,r=8,p=1${'00' * 16}${'11' * 32}"
        self.assertTrue(password_needs_rehash(stored))

    def test_unknown_format_needs_rehash(self):
        for bad in (None, "", "garbage", "argon2id$v=19$m=1$c2FsdA$aGFzaA"):
            with self.subTest(bad=bad):
                self.assertTrue(password_needs_rehash(bad))


class CsrfTokenTests(unittest.TestCase):
    def test_generated_token_is_valid_and_unique(self):
        tokens = {generate_csrf_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            self.assertTrue(is_valid_csrf_token(token))
            self.assertEqual(len(token), 43)

    def test_rejects_malformed_tokens(self):
        for bad in (None, "", "short", "!" * 43, "a" * 42, "a" * 44,
                    "!" * 42, "a" * 43 + "\n", "a" * 42 + "\n", 42,
                    "a" * 21 + "!" + "a" * 21):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_csrf_token(bad))

    def test_accepts_urlsafe_alphabet(self):
        for token in ("a" * 43, "A" * 43, "0" * 43, "-" * 43, "_" * 43):
            with self.subTest(token=token):
                self.assertTrue(is_valid_csrf_token(token))

    def test_verify_requires_both_sides_valid(self):
        token = generate_csrf_token()
        self.assertTrue(verify_csrf_token(token, token))
        self.assertFalse(verify_csrf_token(token, generate_csrf_token()))
        self.assertFalse(verify_csrf_token(None, token))
        self.assertFalse(verify_csrf_token(token, None))
        self.assertFalse(verify_csrf_token("", ""))
        self.assertFalse(verify_csrf_token("x" * 43, token))

    def test_comparison_is_constant_time_helper(self):
        """比对必须走 hmac.compare_digest（此处验证行为等价且不改写入参）。"""
        token = generate_csrf_token()
        other = generate_csrf_token()
        self.assertTrue(verify_csrf_token(token, token[:]))
        self.assertFalse(verify_csrf_token(token, other))


class CookieHeaderTests(unittest.TestCase):
    def _parse(self, header):
        name, _, rest = header[1].decode().partition("=")
        attrs = {}
        value, _, tail = rest.partition(";")
        value = value.strip().strip('"')
        for part in tail.split(";"):
            key, _, attr_value = part.strip().partition("=")
            attrs[key.lower()] = attr_value
        return name, value, attrs

    def test_session_cookie_flags(self):
        name, value, attrs = self._parse(set_cookie_header("tok123", secure=True))
        self.assertEqual(name, "session")
        self.assertEqual(value, "tok123")
        self.assertEqual(attrs.get("path"), "/")
        self.assertIn("httponly", attrs)
        self.assertEqual(attrs.get("samesite"), "Lax")
        self.assertEqual(attrs.get("secure"), "")
        self.assertEqual(attrs.get("max-age"), str(SESSION_MAX_AGE))

    def test_secure_flag_only_when_request_is_https(self):
        _, _, attrs = self._parse(set_cookie_header("tok", secure=False))
        self.assertNotIn("secure", attrs)

    def test_clear_cookie_expires_immediately(self):
        _, value, attrs = self._parse(clear_session_cookie())
        self.assertEqual(value, "")
        self.assertEqual(attrs.get("max-age"), "0")

    def test_csrf_cookie_flags(self):
        name, value, attrs = self._parse(csrf_cookie_header("c" * 43, secure=True))
        self.assertEqual(name, "csrf")
        self.assertEqual(value, "c" * 43)
        self.assertIn("httponly", attrs)
        self.assertEqual(attrs.get("max-age"), str(CSRF_MAX_AGE))

    def test_theme_cookie_value_is_used_verbatim(self):
        name, value, attrs = self._parse(theme_cookie_header("dark"))
        self.assertEqual(name, "theme")
        self.assertEqual(value, "dark")
        self.assertEqual(attrs.get("samesite"), "Lax")

    def test_host_prefix_can_be_enabled_and_disabled(self):
        from elenvind.core.security import configure_cookie_prefix, cookie_names, pick_cookie
        try:
            configure_cookie_prefix(True)
            name, _, _ = self._parse(set_cookie_header("tok"))
            self.assertEqual(name, "__Host-session")
            # 读取侧同时接受带前缀与历史名称
            self.assertEqual(pick_cookie({"__Host-session": "a"}, "session"), "a")
            self.assertEqual(pick_cookie({"session": "b"}, "session"), "b")
            self.assertIn("session", cookie_names("session"))
            # 清理时两个名字都要下发，避免旧 Cookie 残留
            from elenvind.core.security import clear_cookie_headers
            cleared = [self._parse(header)[0] for header in clear_cookie_headers()]
            self.assertEqual(cleared, ["__Host-session", "session"])
        finally:
            configure_cookie_prefix(False)
        name, _, _ = self._parse(set_cookie_header("tok"))
        self.assertEqual(name, "session")


class ImportSideEffectTests(unittest.TestCase):
    def test_security_module_has_no_import_side_effects(self):
        """security.py 导入时不得读取环境变量或文件。

        安全原语必须能在任何环境下被导入与审计；配置校验是 config 的职责。
        """
        source = (PROJECT_ROOT / "elenvind" / "core" / "security.py").read_text(encoding="utf-8")
        # 去掉模块 docstring 后再检查代码体
        body = source.split('"""', 2)[-1]
        code_lines = [line for line in body.splitlines()
                      if not line.strip().startswith("#")]
        code = "\n".join(code_lines)
        self.assertNotIn("os.environ", code)
        self.assertNotIn("getenv", code)

    def test_admin_helpers_respect_config(self):
        from elenvind.core import config as config_module
        from elenvind.core.security import admin_id, is_admin, is_admin_id

        original = dict(config_module.config)
        try:
            config_module.config.clear()
            config_module.config.update({"admin_user_id": 7})
            self.assertEqual(admin_id(), 7)
            self.assertTrue(is_admin({"id": 7}))
            self.assertFalse(is_admin({"id": 1}))
            self.assertTrue(is_admin_id(7))
            self.assertFalse(is_admin_id(1))
            self.assertFalse(is_admin(None))
            self.assertFalse(is_admin({}))

            # 非法/缺失配置 => 无管理员，而不是静默回退到 id=1
            config_module.config.clear()
            self.assertIsNone(admin_id())
            self.assertFalse(is_admin({"id": 1}))
            self.assertFalse(is_admin_id(1))
            config_module.config.update({"admin_user_id": True})
            self.assertIsNone(admin_id())
            config_module.config.update({"admin_user_id": 0})
            self.assertIsNone(admin_id())
        finally:
            config_module.config.clear()
            config_module.config.update(original)


if __name__ == "__main__":
    unittest.main()
