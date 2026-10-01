"""文章索引/正文缓存与自定义页面测试：缓存原子性、热更新、错误隔离、路径安全。"""
import os
import unittest

from tests.support import ElenvindTestCase

from elenvind.features.blog import logic as blog
from elenvind.features.blog.content import (
    ContentError,
    normalize_metadata,
    parse_document,
    validate_slug,
)
from elenvind.features.pages import logic as pages


class ContentFormatTests(unittest.TestCase):
    """TOML front matter + Markdown 的解析契约。"""

    def test_front_matter_is_parsed(self):
        meta, body = parse_document('+++\ntitle = "Hi"\ndate = "2026-01-01"\n+++\n\nBody\n')
        self.assertEqual(meta["title"], "Hi")
        self.assertIn("Body", body)

    def test_document_without_front_matter_is_plain_body(self):
        meta, body = parse_document("just body\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just body\n")

    def test_unclosed_front_matter_raises(self):
        with self.assertRaises(ContentError):
            parse_document('+++\ntitle = "x"\n')

    def test_invalid_toml_raises(self):
        with self.assertRaises(ContentError):
            parse_document("+++\ntitle = \n+++\n")

    def test_crlf_and_missing_trailing_newline(self):
        meta, body = parse_document('+++\r\ntitle = "CRLF"\r\n+++\r\n\r\nBody\r\n')
        self.assertEqual(meta["title"], "CRLF")
        self.assertIn("Body", body)
        meta, body = parse_document('+++\ntitle = "EOF"\n+++')
        self.assertEqual(meta["title"], "EOF")
        self.assertEqual(body, "")

    def test_non_string_input_raises(self):
        with self.assertRaises(ContentError):
            parse_document(None)

    def test_normalize_metadata_requires_title_for_articles(self):
        with self.assertRaises(ContentError):
            normalize_metadata({}, slug="s", require_header=True)
        with self.assertRaises(ContentError):
            normalize_metadata({"date": "2026-01-01"}, slug="s", require_header=True)

    def test_normalize_metadata_normalizes_types(self):
        meta = normalize_metadata({"title": "T", "authors": "Alice"}, slug="s",
                                  require_header=True)
        self.assertEqual(meta["authors"], ["Alice"])
        self.assertEqual(meta["title"], "T")
        self.assertEqual(meta["tags"], [])

    def test_slug_validation(self):
        for good in ("about", "my-page", "my_page", "page123", "a.b"):
            self.assertTrue(validate_slug(good), good)
        for bad in ("..", ".", "a/b", "a\\b", "a b", "", ".hidden", "a\x00b"):
            self.assertFalse(validate_slug(bad), bad)


class ArticleIndexTests(ElenvindTestCase):
    def test_empty_directory(self):
        self.assertEqual(blog.get_articles(), [])

    def test_sorted_by_date_descending(self):
        self.write_article("old", "a", {"title": "Old", "date": "2025-01-01"})
        self.write_article("new", "b", {"title": "New", "date": "2026-06-01"})
        self.assertEqual([a["slug"] for a in blog.get_articles()], ["new", "old"])

    def test_new_article_picked_up_without_restart(self):
        self.write_article("first", "a", {"title": "First", "date": "2026-01-01"})
        self.assertEqual(len(blog.get_articles()), 1)
        path = self.write_article("second", "b", {"title": "Second", "date": "2026-01-02"})
        self.touch(path)
        self.assertEqual(len(blog.get_articles()), 2)

    def test_deleted_article_disappears(self):
        path = self.write_article("gone", "body", {"title": "Gone", "date": "2026-01-01"})
        self.assertIsNotNone(blog.get_article_by_slug("gone"))
        path.unlink()
        self.assertIsNone(blog.get_article_by_slug("gone"))

    def test_broken_article_is_skipped_without_breaking_index(self):
        self.write_article("good", "body", {"title": "Good", "date": "2026-01-01"})
        path = self.articles_dir / "broken.md"
        path.write_text("no front matter", encoding="utf-8")
        self.touch(path)
        self.assertEqual([a["slug"] for a in blog.get_articles()], ["good"])

    def test_binary_file_is_skipped(self):
        self.write_article("good", "body", {"title": "Good", "date": "2026-01-01"})
        path = self.articles_dir / "binary.md"
        path.write_bytes(b"\xff\xfe\x00\x01binary")
        self.touch(path)
        self.assertEqual([a["slug"] for a in blog.get_articles()], ["good"])

    def test_oversized_article_is_skipped(self):
        self.write_article("good", "body", {"title": "Good", "date": "2026-01-01"})
        path = self.articles_dir / "huge.md"
        path.write_text("x" * (blog.MAX_ARTICLE_SIZE + 10), encoding="utf-8")
        self.touch(path)
        self.assertEqual([a["slug"] for a in blog.get_articles()], ["good"])

    def test_cache_reuses_list_when_unchanged(self):
        self.write_article("post", "body", {"title": "P", "date": "2026-01-01"})
        self.assertIs(blog.get_articles(), blog.get_articles())

    def test_failed_scan_keeps_stats_and_index_in_sync(self):
        """索引重建抛异常时不能留下"快照已更新、索引仍旧"的永久错位。"""
        self.write_article("post", "body", {"title": "P", "date": "2026-01-01"})
        blog.load_articles()
        before_stats = dict(blog._file_stats)
        before_articles = list(blog._articles_cache)

        original = blog._load_metadata

        def boom(directory, stats):
            raise RuntimeError("simulated scan failure")

        blog._load_metadata = boom
        try:
            new_path = self.write_article("new", "body",
                                          {"title": "N", "date": "2026-02-01"})
            self.touch(new_path)
            with self.assertRaises(RuntimeError):
                blog.get_articles()
        finally:
            blog._load_metadata = original

        self.assertEqual(blog._file_stats, before_stats)
        self.assertEqual(blog._articles_cache, before_articles)
        self.assertEqual(len(blog.get_articles()), 2)

    def test_slug_lookup_never_touches_filesystem(self):
        self.write_article("post", "body", {"title": "P", "date": "2026-01-01"})
        for slug in ("../config", "../../etc/passwd", "post/../post",
                     "..\\config", "post\x00", ""):
            with self.subTest(slug=slug):
                self.assertIsNone(blog.get_article_by_slug(slug))


class ArticleBodyCacheTests(ElenvindTestCase):
    def test_renders_markdown_body(self):
        self.write_article("post", "**bold** text", {"title": "T", "date": "2026-01-01"})
        meta, html = blog.load_article_body("post")
        self.assertEqual(meta["title"], "T")
        self.assertIn("<strong>bold</strong>", str(html))

    def test_cached_body_is_reused(self):
        self.write_article("post", "body", {"title": "T", "date": "2026-01-01"})
        first = blog.load_article_body("post")
        second = blog.load_article_body("post")
        self.assertIs(first[0], second[0])

    def test_cache_invalidated_on_change(self):
        path = self.write_article("post", "before", {"title": "T", "date": "2026-01-01"})
        self.assertIn("before", str(blog.load_article_body("post")[1]))
        path.write_text('+++\ntitle = "T"\ndate = "2026-01-01"\n+++\n\nafter\n', encoding="utf-8")
        stat = path.stat()
        os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))
        self.assertIn("after", str(blog.load_article_body("post")[1]))

    def test_missing_file_raises_oserror(self):
        """索引里有记录但文件已消失：正文加载必须明确报错，而不是静默返回空。"""
        path = self.write_article("post", "body", {"title": "T", "date": "2026-01-01"})
        self.assertIsNotNone(blog.load_article_body("post"))
        path.unlink()
        # 目录快照变了，下次 get_article_by_slug 就不会再返回该文章 -> 视为不存在
        self.assertIsNone(blog.get_article_by_slug("post"))

    def test_invalid_front_matter_raises_content_error(self):
        path = self.articles_dir / "bad.md"
        path.write_text("+++\nnot valid toml = \n+++\n", encoding="utf-8")
        self.touch(path)
        # 坏文件被索引跳过
        self.assertIsNone(blog.get_article_by_slug("bad"))
        # 但直接解析必须抛出可定位的 ContentError
        with self.assertRaises(ContentError):
            parse_document(path.read_text(encoding="utf-8"))

    def test_raw_html_in_markdown_is_not_executable(self):
        self.write_article("post", "<script>alert(1)</script>\n\nnormal",
                           {"title": "T", "date": "2026-01-01"})
        _meta, html = blog.load_article_body("post")
        self.assertNotIn("<script", str(html))
        self.assertIn("&lt;script&gt;", str(html))


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
        self.assertIn("<!DOCTYPE html>", response.text)     # 带布局的错误页

    def test_article_title_is_escaped(self):
        self.write_article("xss", "body",
                           {"title": "<script>alert(1)</script>", "date": "2026-01-01"})
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
        for index in range(5):
            self.write_article(f"p{index}", "body",
                               {"title": f"Post {index}", "date": f"2026-01-0{index + 1}"})
        self.assertIn("Page 1 of 3", self.app.request("GET", "/").text)
        self.assertIn("Page 3 of 3", self.app.request("GET", "/", query={"page": "3"}).text)
        for page in ("99", "abc", "-5"):
            self.assertEqual(self.app.request("GET", "/", query={"page": page}).status, 200)

    def test_author_list_rendered(self):
        self.write_article("a", "body",
                           {"title": "A", "date": "2026-01-01", "authors": ["Alice", "Bob"]})
        self.assertIn("Alice, Bob", self.app.request("GET", "/article/a").text)


class UserPageTests(ElenvindTestCase):
    def test_page_is_served(self):
        self.write_page("about", "Hello **there**")
        response = self.app.request("GET", "/about")
        self.assertEqual(response.status, 200)
        self.assertIn("<strong>there</strong>", response.text)

    def test_page_hot_reload(self):
        self.write_page("about", "version one")
        self.assertIn("version one", self.app.request("GET", "/about").text)
        path = self.write_page("about", "version two")
        stat = path.stat()
        os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))
        self.assertIn("version two", self.app.request("GET", "/about").text)

    def test_traversal_paths_return_404(self):
        self.write_page("about", "content")
        for path in ("/../config.toml", "/..%2Fconfig.toml", "/about/../secret",
                     "/%2e%2e/config.toml", "/etc/passwd", "/.git/config"):
            with self.subTest(path=path):
                response = self.app.request("GET", path)
                self.assertEqual(response.status, 404)
                self.assertNotIn("site_url", response.text)

    def test_invalid_slug_returns_none(self):
        self.write_page("about", "content")
        for slug in ("..", "../about", "a/b", ""):
            with self.subTest(slug=slug):
                self.assertIsNone(pages.get_page(slug))

    def test_page_cache_atomic_on_failure(self):
        self.write_page("p", "v1")
        pages.load_pages()
        before_stats = dict(pages._file_stats)
        before_pages = dict(pages._pages_cache)
        original = pages._load_pages

        def boom(directory, stats):
            raise RuntimeError("page load failure")

        pages._load_pages = boom
        try:
            with self.assertRaises(RuntimeError):
                pages._rescan()
        finally:
            pages._load_pages = original
        self.assertEqual(pages._file_stats, before_stats)
        self.assertEqual(pages._pages_cache, before_pages)

    def test_page_markdown_is_sanitised(self):
        self.write_page("evil", "<script>alert(1)</script>\n\nok")
        response = self.app.request("GET", "/evil")
        self.assertEqual(response.status, 200)
        self.assertNotIn("<script", response.text)


if __name__ == "__main__":
    unittest.main()
