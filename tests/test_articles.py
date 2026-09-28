"""文章索引/正文缓存与自定义页面测试：缓存提交顺序、热更新、错误隔离、路径安全。"""
import os
import unittest

from tests.support import ElenvindTestCase

from elenvind import articles as articles_module
from elenvind import usrpages as usrpages_module
from elenvind.articles import (
    MAX_ARTICLE_SIZE,
    get_article_by_slug,
    get_articles,
    load_article_content,
    load_articles,
)
from elenvind.evmd_parser import EvmdError
from elenvind.usrpages import get_page, validate_slug


class ArticleIndexTests(ElenvindTestCase):
    def test_empty_directory(self):
        self.assertEqual(get_articles(), [])

    def test_articles_sorted_by_date_descending(self):
        self.write_article("old", "a", {"title": "Old", "date": "2025-01-01"})
        self.write_article("new", "b", {"title": "New", "date": "2026-06-01"})
        self.write_article("mid", "c", {"title": "Mid", "date": "2025-12-01"})
        self.assertEqual([a["slug"] for a in get_articles()], ["new", "mid", "old"])

    def test_article_without_date_sorts_last(self):
        self.write_article("dated", "a", {"title": "Dated", "date": "2026-01-01"})
        self.write_article("undated", "b", {"title": "Undated"})
        self.assertEqual(get_articles()[-1]["slug"], "undated")

    def test_new_article_is_picked_up_without_restart(self):
        self.write_article("first", "a", {"title": "First", "date": "2026-01-01"})
        self.assertEqual(len(get_articles()), 1)
        self.write_article("second", "b", {"title": "Second", "date": "2026-01-02"})
        self.assertEqual(len(get_articles()), 2)

    def test_modified_article_title_is_refreshed(self):
        path = self.write_article("post", "body", {"title": "Before", "date": "2026-01-01"})
        self.assertEqual(get_article_by_slug("post")["title"], "Before")
        path.write_text('@@@\ntitle = "After"\ndate = "2026-01-01"\n@@@\n\nbody\n',
                        encoding="utf-8")
        self.assertEqual(get_article_by_slug("post")["title"], "After")

    def test_deleted_article_disappears(self):
        path = self.write_article("gone", "body", {"title": "Gone", "date": "2026-01-01"})
        self.assertIsNotNone(get_article_by_slug("gone"))
        path.unlink()
        self.assertIsNone(get_article_by_slug("gone"))

    def test_broken_article_is_skipped_without_breaking_index(self):
        self.write_article("good", "body", {"title": "Good", "date": "2026-01-01"})
        (self.articles_dir / "broken.evmd").write_text("no header here", encoding="utf-8")
        self.assertEqual([a["slug"] for a in get_articles()], ["good"])

    def test_binary_file_is_skipped(self):
        self.write_article("good", "body", {"title": "Good", "date": "2026-01-01"})
        (self.articles_dir / "binary.evmd").write_bytes(b"\xff\xfe\x00\x01binary")
        self.assertEqual([a["slug"] for a in get_articles()], ["good"])

    def test_oversized_article_is_skipped(self):
        self.write_article("good", "body", {"title": "Good", "date": "2026-01-01"})
        (self.articles_dir / "huge.evmd").write_text("x" * (MAX_ARTICLE_SIZE + 10),
                                                     encoding="utf-8")
        self.assertEqual([a["slug"] for a in get_articles()], ["good"])

    def test_index_cache_does_not_rescan_when_nothing_changed(self):
        self.write_article("post", "body", {"title": "P", "date": "2026-01-01"})
        first = get_articles()
        second = get_articles()
        self.assertIs(first, second)   # 未变化时复用同一列表对象

    def test_failed_scan_keeps_stats_and_index_in_sync(self):
        """索引重建抛异常时，不能留下"快照已更新、索引仍旧"的永久错位。"""
        self.write_article("post", "body", {"title": "P", "date": "2026-01-01"})
        load_articles()
        before_stats = dict(articles_module._file_stats)
        before_articles = list(articles_module._articles_cache)

        original = articles_module._load_article_metadata

        def boom(dir_path, stats):
            raise RuntimeError("simulated scan failure")

        articles_module._load_article_metadata = boom
        try:
            self.write_article("new", "body", {"title": "N", "date": "2026-02-01"})
            with self.assertRaises(RuntimeError):
                get_articles()
        finally:
            articles_module._load_article_metadata = original

        # 失败后缓存必须原样保留，且下一次调用仍然认为"有变化"从而重试
        self.assertEqual(articles_module._file_stats, before_stats)
        self.assertEqual(articles_module._articles_cache, before_articles)
        self.assertEqual(len(get_articles()), 2)

    def test_slug_lookup_never_touches_filesystem(self):
        self.write_article("post", "body", {"title": "P", "date": "2026-01-01"})
        for slug in ("../config", "../../etc/passwd", "post/../post",
                     "..\\config", "post\x00", ""):
            with self.subTest(slug=slug):
                self.assertIsNone(get_article_by_slug(slug))


class ArticleBodyCacheTests(ElenvindTestCase):
    def test_renders_body(self):
        self.write_article("post", "**bold** text", {"title": "T", "date": "2026-01-01"})
        meta, html = load_article_content("post")
        self.assertEqual(meta["title"], "T")
        self.assertIn("<strong>bold</strong>", html)

    def test_cached_body_is_reused(self):
        self.write_article("post", "body", {"title": "T", "date": "2026-01-01"})
        first_meta, first_html = load_article_content("post")
        second_meta, second_html = load_article_content("post")
        self.assertIs(first_meta, second_meta)
        self.assertEqual(first_html, second_html)

    def test_cache_invalidated_on_change(self):
        path = self.write_article("post", "before", {"title": "T", "date": "2026-01-01"})
        _, html = load_article_content("post")
        self.assertIn("before", html)
        path.write_text('@@@\ntitle = "T"\ndate = "2026-01-01"\n@@@\n\nafter\n',
                        encoding="utf-8")
        _, html = load_article_content("post")
        self.assertIn("after", html)

    def _freeze_index(self, entries):
        """把索引缓存固定为给定条目，并让 _has_changes() 认为目录没变化。

        用于稳定复现"索引已收录、文件随后出问题"的瞬间，不依赖文件系统时序。
        """
        articles_module._articles_cache = entries
        articles_module._file_stats = {}
        original = articles_module._has_changes
        articles_module._has_changes = lambda: False
        self.addCleanup(lambda: setattr(articles_module, "_has_changes", original))

    def test_missing_file_raises_oserror_not_silent_stale(self):
        path = self.write_article("post", "body", {"title": "T", "date": "2026-01-01"})
        load_article_content("post")
        self._freeze_index([{"slug": "post", "file_path": str(path)}])
        path.unlink()
        with self.assertRaises(OSError):
            load_article_content("post")

    def test_invalid_evmd_raises_evmd_error(self):
        path = self.articles_dir / "bad.evmd"
        path.write_text("@@@\nnot valid toml = \n@@@\n", encoding="utf-8")
        self._freeze_index([{"slug": "bad", "file_path": str(path)}])
        with self.assertRaises(EvmdError):
            load_article_content("bad")

    def test_body_cache_not_written_when_file_changes_during_read(self):
        """读取前后状态不一致时不写缓存（避免把半写入内容永久钉住）。"""
        self.write_article("post", "body", {"title": "T", "date": "2026-01-01"})
        path = self.articles_dir / "post.evmd"
        _, first_html = load_article_content("post")
        first_meta = next(a for a in get_articles() if a["slug"] == "post")

        original_read = articles_module._read_with_limit

        def mutating_read(target):
            content = original_read(target)
            # 模拟"读取期间文件被替换"：改内容并显式推进 mtime
            target.write_text('@@@\ntitle = "T2"\ndate = "2026-01-01"\n@@@\n\nnew\n',
                              encoding="utf-8")
            stat = target.stat()
            os.utime(target, (stat.st_atime + 5, stat.st_mtime + 5))
            return content

        # 固定索引缓存，避免重扫遮蔽掉"这次读取"的行为
        self._freeze_index([first_meta])
        articles_module._body_cache.pop(str(path), None)
        articles_module._read_with_limit = mutating_read
        try:
            meta, _ = load_article_content("post")
        finally:
            articles_module._read_with_limit = original_read

        self.assertEqual(meta["title"], "T")
        self.assertNotIn(str(path), articles_module._body_cache)


class ArticleHttpTests(ElenvindTestCase):
    def test_article_page_renders(self):
        self.write_article("hello", "Body **text**", {"title": "Hello", "date": "2026-01-01"})
        response = self.app.request("GET", "/article/hello")
        self.assertEqual(response.status, 200)
        self.assertIn("Hello", response.text)
        self.assertIn("<strong>text</strong>", response.text)

    def test_missing_article_returns_404(self):
        response = self.app.request("GET", "/article/nope")
        self.assertEqual(response.status, 404)

    def test_article_with_syntax_error_renders_500_page_not_crash(self):
        (self.articles_dir / "broken.evmd").write_text(
            '@@@\ntitle = "B"\ndate = "2026-01-01"\n@@@\n\n```\nunclosed', encoding="utf-8")
        load_articles()
        response = self.app.request("GET", "/article/broken")
        self.assertIn(response.status, (200, 500))

    def test_article_title_is_escaped(self):
        self.write_article("xss", "body",
                           {"title": '<script>alert(1)</script>', "date": "2026-01-01"})
        response = self.app.request("GET", "/article/xss")
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_home_lists_articles(self):
        self.write_article("a", "body", {"title": "Alpha", "date": "2026-01-01"})
        response = self.app.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("Alpha", response.text)
        self.assertIn("/article/a", response.text)

    def test_home_pagination(self):
        self._config["pagination"] = {"per_page": 2}
        for i in range(5):
            self.write_article(f"p{i}", "body",
                               {"title": f"Post {i}", "date": f"2026-01-0{i + 1}"})
        first = self.app.request("GET", "/")
        self.assertIn("Page 1 of 3", first.text)
        last = self.app.request("GET", "/", query={"page": "3"})
        self.assertIn("Page 3 of 3", last.text)
        # 越界页码收敛到合法范围，不报错
        self.assertEqual(self.app.request("GET", "/", query={"page": "99"}).status, 200)
        self.assertEqual(self.app.request("GET", "/", query={"page": "abc"}).status, 200)
        self.assertEqual(self.app.request("GET", "/", query={"page": "-5"}).status, 200)


class UserPageTests(ElenvindTestCase):
    def test_page_is_served(self):
        self.write_page("about", "Hello **there**")
        response = self.app.request("GET", "/about")
        self.assertEqual(response.status, 200)
        self.assertIn("<strong>there</strong>", response.text)

    def test_page_hot_reload(self):
        self.write_page("about", "version one")
        self.assertIn("version one", self.app.request("GET", "/about").text)
        self.write_page("about", "version two")
        self.assertIn("version two", self.app.request("GET", "/about").text)

    def test_invalid_slugs_are_rejected_without_filesystem_access(self):
        for slug in ("..", ".", "a/b", "a\\b", "con:name", "a\x00b", "",
                     "page with space", "%2e%2e"):
            with self.subTest(slug=slug):
                self.assertFalse(validate_slug(slug))

    def test_valid_slug_shapes(self):
        for slug in ("about", "my-page", "my_page", "page123", "a.b"):
            with self.subTest(slug=slug):
                self.assertTrue(validate_slug(slug))

    def test_traversal_paths_return_404(self):
        self.write_page("about", "content")
        for path in ("/../config.toml", "/..%2Fconfig.toml", "/about/../secret",
                     "/%2e%2e/config.toml", "/etc/passwd", "/.git/config"):
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertEqual(response.status, 404)
                self.assertNotIn("site_url", response.text)

    def test_page_slug_is_escaped_in_title(self):
        self.write_page("about", "content")
        response = self.app.request("GET", "/about")
        self.assertIn("<title>about - Test Site</title>", response.text)

    def test_get_page_returns_none_for_invalid_slug(self):
        self.write_page("about", "content")
        for slug in ("..", "../about", "a/b", ""):
            with self.subTest(slug=slug):
                self.assertIsNone(get_page(slug))


if __name__ == "__main__":
    unittest.main()
