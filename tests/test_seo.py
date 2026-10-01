"""SEO 元文件测试：site_url 来源、禁止 Host 头投毒、XML/robots 内容与缓存头。"""
import unittest
from xml.etree import ElementTree

from tests.support import ElenvindTestCase


class RobotsTests(ElenvindTestCase):
    def test_robots_points_to_configured_site(self):
        response = self.app.request("GET", "/robots.txt")
        self.assertEqual(response.status, 200)
        self.assertIn("User-agent: *", response.text)
        self.assertIn("Allow: /", response.text)
        self.assertIn("Sitemap: https://example.com/sitemap.xml", response.text)

    def test_robots_cache_header(self):
        response = self.app.request("GET", "/robots.txt")
        self.assertEqual(response.header("cache-control"), "public, max-age=3600")
        self.assertTrue(response.content_type.startswith("text/plain"))

    def test_host_header_cannot_poison_robots(self):
        """伪造 Host 不得改变 robots 里的站点地址（Host 头投毒防护）。"""
        for host in ("evil.example.org", "localhost:9999", "attacker.test",
                     "example.com@evil.test", "127.0.0.1"):
            with self.subTest(host=host):
                response = self.app.raw_request(
                    "GET", "/robots.txt", headers=[("host", host)])
                self.assertIn("Sitemap: https://example.com/sitemap.xml", response.text)
                self.assertNotIn(host, response.text)

    def test_missing_site_url_omits_sitemap_line(self):
        self._config["site_url"] = ""
        response = self.app.request("GET", "/robots.txt")
        self.assertNotIn("Sitemap:", response.text)


class SitemapTests(ElenvindTestCase):
    def test_sitemap_contains_home_and_articles(self):
        self.write_article("alpha", "body", {"title": "Alpha", "date": "2026-01-01"})
        self.write_article("beta", "body", {"title": "Beta", "date": "2026-02-01"})
        response = self.app.request("GET", "/sitemap.xml")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("cache-control"), "public, max-age=600")
        self.assertTrue(response.content_type.startswith("application/xml"))
        root = ElementTree.fromstring(response.text)
        locations = [node.text for node in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertEqual(locations, ["https://example.com/",
                                     "https://example.com/article/beta",
                                     "https://example.com/article/alpha"])

    def test_sitemap_is_valid_xml_with_lastmod(self):
        self.write_article("alpha", "body", {"title": "Alpha", "date": "2026-01-01"})
        response = self.app.request("GET", "/sitemap.xml")
        root = ElementTree.fromstring(response.text)   # 解析失败即测试失败
        lastmods = [node.text for node in root.iter(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")]
        self.assertEqual(lastmods, ["2026-01-01"])

    def test_host_header_cannot_poison_sitemap(self):
        self.write_article("alpha", "body", {"title": "Alpha", "date": "2026-01-01"})
        response = self.app.raw_request("GET", "/sitemap.xml",
                                        headers=[("host", "evil.example.org")])
        self.assertIn("https://example.com/article/alpha", response.text)
        self.assertNotIn("evil.example.org", response.text)

    def test_missing_site_url_yields_empty_urlset(self):
        self._config["site_url"] = ""
        self.write_article("alpha", "body", {"title": "Alpha", "date": "2026-01-01"})
        response = self.app.request("GET", "/sitemap.xml")
        self.assertNotIn("<loc>", response.text)
        ElementTree.fromstring(response.text)

    def test_slug_with_special_characters_is_percent_encoded(self):
        self.write_article("weird slug", "body", {"title": "W", "date": "2026-01-01"})
        response = self.app.request("GET", "/sitemap.xml")
        self.assertIn("weird%20slug", response.text)

    def test_article_metadata_is_xml_escaped(self):
        self.write_article("alpha", "body", {"title": "A & B <script>", "date": "2026-01-01"})
        response = self.app.request("GET", "/sitemap.xml")
        ElementTree.fromstring(response.text)


class CanonicalHostPolicyTests(ElenvindTestCase):
    def test_seo_module_does_not_read_host_header(self):
        """静态检查：SEO 模块不得从 Host 头推导站点地址（Host 头投毒）。"""
        from tests.support import PROJECT_ROOT
        source = (PROJECT_ROOT / "elenvind" / "features" / "seo"
                  / "routes.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]
        self.assertNotIn("host", body.lower())
        self.assertIn("site_url", body)


if __name__ == "__main__":
    unittest.main()
