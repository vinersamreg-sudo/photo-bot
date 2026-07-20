import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
PUBLIC = SITE / "public"
MAX_URL = "https://max.ru/se13572368_bot"
LEGAL = (
    "legal/offer.html",
    "legal/privacy.html",
    "legal/personal-data.html",
    "legal/payment-refund.html",
    "legal/terms.html",
)


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.headings: list[tuple[str, str]] = []
        self.meta: list[dict[str, str]] = []
        self._heading: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values)
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


def load(path: str) -> str:
    return (PUBLIC / path).read_text(encoding="utf-8")


class PixoraSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = load("index.html")
        cls.pages = {path: load(path) for path in ("index.html", "contacts.html", *LEGAL)}
        cls.parsers: dict[str, PageParser] = {}
        for path, html in cls.pages.items():
            parser = PageParser()
            parser.feed(html)
            cls.parsers[path] = parser

    # 01
    def test_home_has_single_pixora_h1(self) -> None:
        h1 = [text for level, text in self.parsers["index.html"].headings if level == "h1"]
        self.assertEqual(h1, ["Pixora AI"])

    def test_public_home_has_no_runtime_javascript(self) -> None:
        self.assertNotIn("<script src=", self.html)
        self.assertFalse((PUBLIC / "app.js").exists())

    # 02
    def test_required_product_sections_exist(self) -> None:
        for section_id in ("possibilities", "examples", "how", "faq"):
            self.assertIn(f'id="{section_id}"', self.html)

    # 03
    def test_required_product_copy_exists(self) -> None:
        for text in ("Изменяем фотографии", "Деловое фото", "Фото на памятник", "Свой сценарий"):
            self.assertIn(text, self.html)

    # 04
    def test_closed_pilot_is_disclosed(self) -> None:
        self.assertGreaterEqual(self.html.count("Закрытое тестирование"), 1)
        self.assertIn("Платежи отключены", self.html)

    # 05
    def test_price_is_49_rubles_for_continuation_pack(self) -> None:
        self.assertIn("49 ₽", self.html)
        self.assertIn("Ещё 2 варианта + 1 оригинал", self.html)
        self.assertIn("право выбрать 1 оригинал", self.html)
        forbidden_price = f"{3 * 50 - 1} ₽"
        self.assertNotIn(forbidden_price, self.html)

    # 06
    def test_no_subscription_or_autopay_claim(self) -> None:
        self.assertIn("Без подписки", self.html)
        self.assertIn("Нет регулярных или автоматических списаний", self.html)

    # 07
    def test_ai_limitations_are_visible(self) -> None:
        self.assertIn("не обещаем неизменность каждой детали", self.html)
        self.assertIn("AI может затронуть", self.html)

    # 08
    def test_does_not_sell_provider_model(self) -> None:
        lowered = self.html.lower()
        for prohibited in ("gpt image", "gpt-image", "openai", "midjourney", "kandinsky"):
            self.assertNotIn(prohibited, lowered)

    # 09
    def test_verified_max_links_are_used(self) -> None:
        max_links = [link for link in self.parsers["index.html"].links if "max-link" in link.get("class", "")]
        self.assertGreaterEqual(len(max_links), 5)
        self.assertTrue(all(link["href"] == MAX_URL for link in max_links))

    # 10
    def test_no_generic_max_placeholder(self) -> None:
        self.assertNotIn('href="https://max.ru/"', "\n".join(self.pages.values()))

    # 11
    def test_external_links_are_safe(self) -> None:
        for parser in self.parsers.values():
            for link in parser.links:
                if link["href"].startswith("http"):
                    self.assertEqual(link.get("target"), "_blank")
                    self.assertIn("noopener", link.get("rel", ""))
                    self.assertIn("noreferrer", link.get("rel", ""))

    # 12
    def test_images_are_local_and_accessible(self) -> None:
        images = self.parsers["index.html"].images
        self.assertGreaterEqual(len(images), 4)
        for image in images:
            self.assertTrue(image.get("src", "").startswith("/assets/"))
            self.assertTrue(image.get("alt", "").strip())
            self.assertTrue(image.get("width", "").isdigit())
            self.assertTrue(image.get("height", "").isdigit())

    # 13
    def test_images_are_optimized(self) -> None:
        webp = list((PUBLIC / "assets").glob("*.webp"))
        self.assertEqual(len(webp), 9)
        self.assertLess(max(path.stat().st_size for path in webp), 100_000)

    # 14
    def test_hero_is_preloaded_and_eager(self) -> None:
        self.assertIn('href="/assets/hero-840.webp" as="image"', self.html)
        self.assertIn('fetchpriority="high" loading="eager" decoding="sync"', self.html)

    # 15
    def test_assets_have_documented_provenance(self) -> None:
        provenance = (SITE / "docs" / "VISUAL_ASSETS.md").read_text(encoding="utf-8")
        self.assertIn("image generation tool", provenance)
        self.assertIn("чужие или пользовательские фотографии не использованы", provenance)

    # 16
    def test_every_page_has_one_h1(self) -> None:
        for path, parser in self.parsers.items():
            self.assertEqual(len([x for x in parser.headings if x[0] == "h1"]), 1, path)

    # 17
    def test_every_page_has_utf8_and_viewport(self) -> None:
        for path, html in self.pages.items():
            self.assertIn('<meta charset="utf-8">', html, path)
            self.assertIn('name="viewport"', html, path)

    # 18
    def test_every_page_has_unique_title(self) -> None:
        titles = [re.search(r"<title>(.*?)</title>", html, re.S).group(1) for html in self.pages.values()]
        self.assertEqual(len(titles), len(set(titles)))

    # 19
    def test_every_page_has_canonical(self) -> None:
        for path, html in self.pages.items():
            expected = "https://pixoraai.ru/" if path == "index.html" else f"https://pixoraai.ru/{path}"
            self.assertIn(f'rel="canonical" href="{expected}"', html, path)

    # 20
    def test_home_has_open_graph_metadata(self) -> None:
        for prop in ("og:type", "og:site_name", "og:title", "og:description", "og:url", "og:image"):
            self.assertIn(f'property="{prop}"', self.html)

    # 21
    def test_home_has_faq_structured_data(self) -> None:
        self.assertIn('"@type": "FAQPage"', self.html)
        self.assertIn('"@type": "WebApplication"', self.html)

    # 22
    def test_unavailable_service_has_no_offer_schema(self) -> None:
        self.assertNotRegex(self.html, r'"@type"\s*:\s*"Offer"')

    # 23
    def test_favicon_and_manifest_exist(self) -> None:
        self.assertTrue((PUBLIC / "assets" / "favicon.svg").is_file())
        self.assertIn('rel="manifest" href="/site.webmanifest"', self.html)

    # 24
    def test_manifest_is_valid(self) -> None:
        manifest = json.loads(load("site.webmanifest"))
        self.assertEqual(manifest["name"], "Pixora AI")
        self.assertEqual(manifest["start_url"], "/")
        self.assertTrue(manifest["icons"])

    # 25
    def test_robots_allows_public_pages(self) -> None:
        robots = load("robots.txt")
        self.assertIn("Allow: /", robots)
        self.assertNotIn("Disallow: /legal", robots)
        self.assertIn("https://pixoraai.ru/sitemap.xml", robots)

    # 26
    def test_sitemap_lists_all_public_pages(self) -> None:
        sitemap = load("sitemap.xml")
        for path in ("", "contacts.html", *LEGAL):
            self.assertIn(f"https://pixoraai.ru/{path}", sitemap)

    # 27
    def test_all_internal_links_resolve(self) -> None:
        for source, parser in self.parsers.items():
            for link in parser.links:
                href = link["href"]
                parsed = urlparse(href)
                if parsed.scheme or href.startswith("#"):
                    continue
                target = PUBLIC / (parsed.path.lstrip("/") or "index.html")
                self.assertTrue(target.is_file(), f"{source}: {href}")

    # 28
    def test_all_legal_pages_are_linked_from_home(self) -> None:
        hrefs = {link["href"] for link in self.parsers["index.html"].links}
        for path in LEGAL:
            self.assertIn(f"/{path}", hrefs)
        self.assertIn("/contacts.html", hrefs)

    # 29
    def test_seller_identity_is_public(self) -> None:
        contacts = self.pages["contacts.html"]
        self.assertIn("Нурмухамитов Винер Табризович", contacts)
        self.assertIn("Самозанятый", contacts)
        self.assertIn("налога на профессиональный доход", contacts)
        self.assertIn("Самара", contacts)
        self.assertIn("631937938795", contacts)
        self.assertIn("viner-89@mail.ru", contacts)

    # 30
    def test_only_verified_email_is_published(self) -> None:
        public_text = "\n".join(self.pages.values()).lower()
        self.assertNotIn("hello@pixoraai.ru", public_text)
        self.assertNotIn("vinersamreg@gmail.com", public_text)
        self.assertIn("mailto:viner-89@mail.ru", public_text)

    # 31
    def test_offer_has_at_least_35_numbered_sections(self) -> None:
        offer = self.pages["legal/offer.html"]
        numbers = re.findall(r"<h2>(\d+)\.", offer)
        self.assertGreaterEqual(len(numbers), 35)
        self.assertEqual(len(numbers), len(set(numbers)))

    # 32
    def test_offer_has_price_delivery_and_refund(self) -> None:
        offer = self.pages["legal/offer.html"]
        for text in ("49 ₽", "доставляются в диалог", "Возврат", "Robokassa", "не является подпиской"):
            self.assertIn(text, offer)

    # 33
    def test_privacy_discloses_real_data_flow(self) -> None:
        privacy = self.pages["legal/privacy.html"]
        for text in ("идентификатор пользователя", "фотографии", "OpenAI", "Hetzner", "GitHub", "Robokassa", "Трансграничная"):
            self.assertIn(text, privacy)

    # 34
    def test_privacy_discloses_retention_and_cookies(self) -> None:
        privacy = self.pages["legal/privacy.html"]
        for text in ("до 30 дней", "до 180 дней", "24 часов", "не устанавливает cookies"):
            self.assertIn(text, privacy)

    # 35
    def test_consent_matches_actual_save_semantics(self) -> None:
        consent = self.pages["legal/personal-data.html"]
        self.assertIn("только после фактического успешного сохранения и проверки", consent)
        self.assertIn("Неуспешная или отклонённая загрузка", consent)

    # 36
    def test_payment_page_is_truthful(self) -> None:
        payment = self.pages["legal/payment-refund.html"]
        self.assertIn("Оплата сейчас отключена", payment)
        self.assertIn("49 ₽", payment)
        self.assertIn("Полностью неиспользованный пакет", payment)
        self.assertIn("рассматривается вручную", payment)

    # 37
    def test_terms_cover_abuse_and_rights(self) -> None:
        terms = self.pages["legal/terms.html"]
        for text in ("Права на фотографии", "Запрещено", "Abuse", "водяной знак", "Удаление"):
            self.assertIn(text, terms)

    # 38
    def test_no_draft_placeholder_or_fake_social_proof(self) -> None:
        public_text = "\n".join(self.pages.values()).lower()
        for prohibited in ("lorem", "черновик", "заглуш", "отзывы клиентов", "рейтинг 5", "10 000 пользователей"):
            self.assertNotIn(prohibited, public_text)

    # 39
    def test_no_payment_button_while_disabled(self) -> None:
        public_text = "\n".join(self.pages.values())
        for prohibited in (">Оплатить<", ">Купить<", "payment-link", "robokassa.ru/Merchant"):
            self.assertNotIn(prohibited, public_text)

    # 40
    def test_public_tree_has_no_secret_or_private_path(self) -> None:
        patterns = ("sk-proj-", "BEGIN PRIVATE KEY", "MAX_BOT_TOKEN", "OPENAI_API_KEY", "ROBOKASSA_PASSWORD", "C:\\\\Users", "/opt/photo-bot", "127.0.0.1", "localhost")
        for path in PUBLIC.rglob("*"):
            if not path.is_file() or path.suffix.lower() in {".webp", ".png", ".jpg", ".jpeg", ".ico"}:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                self.assertNotIn(pattern, text, str(path))

    # 41
    def test_no_card_numbers_or_bank_requisites(self) -> None:
        public_text = "\n".join(self.pages.values())
        self.assertNotRegex(public_text, r"(?:\d[ -]?){16}")
        self.assertNotRegex(public_text, r"(?:р/с|к/с|БИК)\s*[:№]")

    # 42
    def test_no_source_maps_or_directory_indexes(self) -> None:
        self.assertFalse(list(PUBLIC.rglob("*.map")))
        self.assertFalse(list(PUBLIC.rglob(".env")))
        self.assertFalse(list(PUBLIC.rglob("*.sqlite*")))
        self.assertFalse(list(PUBLIC.rglob("*.log")))

    # 43
    def test_nginx_has_required_security_headers(self) -> None:
        nginx = (SITE / "nginx" / "pixoraai.ru.conf").read_text(encoding="utf-8")
        for header in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy", "frame-ancestors"):
            self.assertIn(header, nginx)

    # 44
    def test_nginx_serves_only_site_tree(self) -> None:
        nginx = (SITE / "nginx" / "pixoraai.ru.conf").read_text(encoding="utf-8")
        self.assertIn("root /opt/pixora-site/current", nginx)
        self.assertNotIn("proxy_pass", nginx)
        self.assertNotIn("/opt/photo-bot", nginx)

    # 45
    def test_nginx_denies_sensitive_extensions(self) -> None:
        nginx = (SITE / "nginx" / "pixoraai.ru.conf").read_text(encoding="utf-8")
        for value in (".env", ".git", "sqlite", "log", "bak", "map"):
            self.assertIn(value, nginx)

    # 46
    def test_site_workflow_is_gated_and_atomic(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")
        self.assertIn("PIXORA_SITE_DEPLOY_ENABLED == 'true'", workflow)
        self.assertIn("/opt/pixora-site/releases/$GITHUB_SHA", workflow)
        self.assertIn("BASE=/opt/pixora-site", workflow)
        self.assertIn("$BASE/current", workflow)
        self.assertIn("$BASE/previous", workflow)

    def test_lighthouse_keeps_network_emulation_without_flaky_cpu_multiplier(self) -> None:
        package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
        for name in ("lighthouse:mobile", "lighthouse:desktop"):
            command = package["scripts"][name]
            self.assertIn("--throttling.cpuSlowdownMultiplier=1", command)
            self.assertNotIn("--throttling-method=provided", command)

    # 47
    def test_site_workflow_does_not_touch_backend(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")
        self.assertNotIn("systemctl restart photo-bot", workflow)
        self.assertNotIn("/opt/photo-bot", workflow)


if __name__ == "__main__":
    unittest.main()
