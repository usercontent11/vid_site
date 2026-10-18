import requests
import unittest
from extractor import apply_cookie_secret, detect_access_status, make_page_path, normalize_card, path_only, slugify


class ExtractorTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("Old & Young"), "old-young")
        self.assertEqual(slugify("18 Year Old"), "18-year-old")

    def test_pagination(self):
        self.assertEqual(make_page_path("/categories/erotica", 1), "/categories/erotica")
        self.assertEqual(make_page_path("/categories/erotica", 2), "/categories/erotica/2")

    def test_path_only(self):
        self.assertEqual(path_only("https://mobilexh.live/videos/a?x=1#f"), "/videos/a")
        self.assertEqual(path_only("https://cdn.example/a.webp?q=2"), "/a.webp")

    def test_normalization(self):
        card = {"id": 1, "title": " T ", "pageURL": "https://mobilexh.live/videos/t",
          "duration": 8, "created": 1700000000, "views": 2, "videoType": "video", "thumbId": 7,
          "thumbURL": "https://cdn.example/t.webp",
          "spriteURL": "https://thumb-v6.cdnsolutions.media/s.webp"}
        row = normalize_card(card)
        self.assertEqual(row["title"], "T")
        self.assertEqual(row["page_path"], "/videos/t")
        self.assertEqual(row["trailer_cdn_version"], 6)
        self.assertEqual(row["access_status"], "unknown")
        self.assertIsNone(normalize_card({**card, "isPremium": True}))

    def test_category_page_context_accepts_matching_slug_even_if_internal_type_differs(self):
        from extractor import validate_category_context
        data = {"currentPage": "categoryPage"}
        category = {"category_name": "Amateur", "category_slug": "amateur", "category_path": "/categories/amateur"}
        channel = {"tagType": "tag", "slug": "amateur", "inurl": "amateur"}
        validate_category_context(data, category, "/categories/amateur", channel, [{"id": 1}])

    def test_category_page_context_reports_mismatched_slug(self):
        from extractor import SourceFormatError, validate_category_context
        data = {"currentPage": "categoryPage"}
        category = {"category_name": "Algerian", "category_slug": "algerian", "category_path": "/categories/algerian"}
        channel = {"tagType": "category", "slug": "amateur", "inurl": "amateur"}
        with self.assertRaisesRegex(SourceFormatError, "Requested category slug"):
            validate_category_context(data, category, "/categories/algerian", channel, [{"id": 1}])

    def test_default_age_cookie(self):
        session = requests.Session()
        apply_cookie_secret(session)
        values = {c.name: c.value for c in session.cookies}
        self.assertEqual(values["parental-control"], "yes")
        self.assertEqual(values["x_content_preference_index"], "straight")
        self.assertNotIn("g_state", values)


if __name__ == "__main__":
    unittest.main()
