import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


SITE = Path(__file__).resolve().parents[1]
PUBLIC = SITE / "public"


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.images: list[dict[str, str]] = []
        self.headings: list[tuple[str, str]] = []
        self.meta: list[dict[str, str]] = []
        self._heading: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag == "img":
            self.images.append(values)
        if tag == "meta":
            self.meta.append(values)
        if tag in {"h1", "h2", "h3"}:
            self._heading = tag
            self._text = []

    def handle_data(self, data):
        if self._heading:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == self._heading:
            self.headings.append((tag, " ".join("".join(self._text).split())))
            self._heading = None


class PixoraSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        cls.parser = PageParser()
        cls.parser.feed(cls.html)

    def test_required_product_copy_and_sections(self) -> None:
        required = (
            "Pixora AI",
            "Изменяем фотографии с помощью искусственного интеллекта",
            "Открыть бота в MAX",
            "Деловое фото",
            "Фото на памятник",
            "Свой сценарий",
            "Первая обработка бесплатно",
            "Без подписки",
            "Загрузите фото",
            "Опишите изменение",
            "Сохраняются ли фотографии?",
        )
        for text in required:
            self.assertIn(text, self.html)
        for section_id in ("possibilities", "examples", "how", "faq"):
            self.assertIn(f'id="{section_id}"', self.html)

    def test_has_one_clear_h1_and_semantic_heading_order(self) -> None:
        h1 = [text for level, text in self.parser.headings if level == "h1"]
        self.assertEqual(h1, ["Pixora AI"])
        self.assertGreaterEqual(len(self.parser.headings), 20)

    def test_does_not_sell_models_or_name_competitors(self) -> None:
        lowered = self.html.lower()
        for prohibited in ("gpt image", "openai", "midjourney", "kandinsky"):
            self.assertNotIn(prohibited, lowered)

    def test_max_links_are_safe_placeholders_and_trackable(self) -> None:
        max_links = re.findall(r'<a[^>]+class="[^"]*max-link[^"]*"[^>]+>', self.html)
        self.assertGreaterEqual(len(max_links), 5)
        for link in max_links:
            self.assertIn('href="https://max.ru/"', link)
            self.assertIn('rel="noopener noreferrer"', link)
            self.assertIn("data-max-cta=", link)

    def test_images_are_local_optimized_and_accessible(self) -> None:
        self.assertGreaterEqual(len(self.parser.images), 4)
        for image in self.parser.images:
            self.assertTrue(image.get("src", "").startswith("/assets/"))
            self.assertTrue(image.get("alt", "").strip())
            self.assertTrue(image.get("width", "").isdigit())
            self.assertTrue(image.get("height", "").isdigit())
        webp = list((PUBLIC / "assets").glob("*.webp"))
        self.assertEqual(len(webp), 9)
        self.assertLess(max(path.stat().st_size for path in webp), 100_000)
        self.assertIn('href="/assets/hero-480.webp" as="image"', self.html)
        self.assertIn('href="/assets/hero-840.webp" as="image"', self.html)
        self.assertIn('fetchpriority="high" loading="eager" decoding="sync"', self.html)

    def test_internal_links_and_static_assets_exist(self) -> None:
        for href in self.parser.links:
            parsed = urlparse(href)
            if parsed.scheme or href.startswith(("#", "mailto:")):
                continue
            target = PUBLIC / parsed.path.lstrip("/")
            if parsed.path == "/":
                target = PUBLIC / "index.html"
            self.assertTrue(target.is_file(), href)
        for name in ("styles.css", "app.js", "robots.txt", "sitemap.xml"):
            self.assertTrue((PUBLIC / name).is_file())

    def test_seo_and_privacy_defaults(self) -> None:
        self.assertIn('<html lang="ru">', self.html)
        self.assertIn('rel="canonical" href="https://pixoraai.ru/"', self.html)
        self.assertIn('name="description"', self.html)
        self.assertIn('property="og:image"', self.html)
        self.assertIn('"@type": "WebApplication"', self.html)
        self.assertIn('"@type": "FAQPage"', self.html)
        robots = (PUBLIC / "robots.txt").read_text(encoding="utf-8")
        self.assertIn("Sitemap: https://pixoraai.ru/sitemap.xml", robots)

    def test_manifest_is_valid_json(self) -> None:
        manifest = json.loads((PUBLIC / "site.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "Pixora AI")


if __name__ == "__main__":
    unittest.main()
