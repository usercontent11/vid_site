import requests
import unittest
from extractor import apply_cookie_secret, detect_access_status, make_page_path, normalize_card, parse_category_catalog, path_only, slugify


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

    def test_grouped_category_json_uses_href_and_merges_duplicate_groups(self):
        payload = {"total_categories": 3, "groups": [
            {"group":"trending","categories":[
                {"category":"American","href":"https://mobilexh.live/categories/american"},
                {"category":"18 Year Old","href":"https://mobilexh.live/categories/18-year-old"}]},
            {"group":"ethnicity","categories":[
                {"category":"American","href":"https://mobilexh.live/categories/american"}]}
        ]}
        categories = parse_category_catalog(payload)
        self.assertEqual(len(categories), 2)
        american = next(item for item in categories if item["category_slug"] == "american")
        self.assertEqual(american["category_path"], "/categories/american")
        self.assertEqual(american["category_groups"], ["trending", "ethnicity"])

    def test_grouped_category_json_rejects_other_host(self):
        payload = {"groups":[{"group":"test","categories":[
            {"category":"Example","href":"https://example.org/categories/example"}]}]}
        with self.assertRaisesRegex(RuntimeError, "uses host"):
            parse_category_catalog(payload)

    def test_default_age_cookie(self):
        session = requests.Session()
        # Explicit empty input selects defaults even when Actions has SOURCE_COOKIE set.
        apply_cookie_secret(session, cookie_text="")
        values = {c.name: c.value for c in session.cookies}
        self.assertEqual(values["parental-control"], "yes")
        self.assertEqual(values["x_content_preference_index"], "straight")
        self.assertNotIn("g_state", values)

    def test_raw_g_state_json_cookie_value(self):
        raw = '{"i_l":0,"i_ll":1792304355741,"i_b":"example-test-value",'
        raw += '"i_e":{"enable_itp_optimization":24},"i_et":1792304355741}'
        session = requests.Session()
        apply_cookie_secret(session, cookie_text=raw)
        values = {c.name: c.value for c in session.cookies}
        self.assertIn("g_state", values)
        self.assertIn('"i_b":"example-test-value"', values["g_state"])
        self.assertEqual(values["parental-control"], "yes")
        self.assertEqual(values["x_content_preference_index"], "straight")


if __name__ == "__main__":
    unittest.main()
