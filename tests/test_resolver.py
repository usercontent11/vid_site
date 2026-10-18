import unittest
from unittest.mock import patch

from resolver import StreamResolutionError, extract_m3u8_url, source_page_url, _read_source_page


class ResolverTests(unittest.TestCase):
    def test_builds_source_page_from_path(self):
        self.assertEqual(
            source_page_url("/videos/example/", "https://mobilexh.live"),
            "https://mobilexh.live/videos/example/",
        )

    def test_rejects_absolute_source_url(self):
        with self.assertRaises(StreamResolutionError):
            source_page_url("https://evil.test/videos/x", "https://mobilexh.live")

    def test_rejects_non_video_path(self):
        with self.assertRaises(StreamResolutionError):
            source_page_url("/categories/x", "https://mobilexh.live")

    def test_extracts_manifest_from_unlisted_cdn(self):
        page = r'<script>src="https:\/\/random-stream.cdnexample.net\/hls\/master.m3u8?token=abc\u0026v=1"</script>'
        self.assertEqual(
            extract_m3u8_url(page, "https://mobilexh.live/videos/x/"),
            "https://random-stream.cdnexample.net/hls/master.m3u8?token=abc&v=1",
        )

    def test_rejects_non_https_manifest(self):
        page = '<script src="http://random-stream.cdnexample.net/master.m3u8"></script>'
        with self.assertRaises(StreamResolutionError):
            extract_m3u8_url(page, "https://mobilexh.live/videos/x/")

    def test_rejects_local_manifest_ip(self):
        page = '<script src="https://127.0.0.1/master.m3u8"></script>'
        with self.assertRaises(StreamResolutionError):
            extract_m3u8_url(page, "https://mobilexh.live/videos/x/")


    def test_extracts_source_preload_link_without_m3u8_suffix(self):
        page = '''
        <html><head>
        <link rel="preload" as="fetch" crossorigin href="https://media.example-cdn.net/hls/master?token=abc">
        </head><body><div id="player"></div></body></html>
        '''
        self.assertEqual(
            extract_m3u8_url(page, "https://mobilexh.live/videos/x/"),
            "https://media.example-cdn.net/hls/master?token=abc",
        )

    def test_rejects_unsafe_preload_link(self):
        page = '''
        <link rel="preload" as="fetch" crossorigin href="https://127.0.0.1/hls/master">
        '''
        with self.assertRaises(StreamResolutionError):
            extract_m3u8_url(page, "https://mobilexh.live/videos/x/")


    def test_inspects_page_structure_without_exposing_query_values(self):
        from resolver import inspect_page_structure
        page = """<html><head><title>Test video</title>
        <link rel="preload" as="fetch" crossorigin href="https://stream.example/hls/master?token=SECRET123">
        <script id="initials-script">window.initials = {"videoModel":{"player":{"manifestUrl":"https://stream.example/a.m3u8"}}};</script>
        </head><body><video controls></video></body></html>"""
        report = inspect_page_structure(page, "https://mobilexh.live/videos/test/")
        self.assertEqual(report["page"]["title"], "Test video")
        self.assertTrue(report["page"]["has_initials_script"])
        self.assertEqual(report["keyword_counts"]["m3u8"], 1)
        link = report["links"][0]
        self.assertTrue(link["crossorigin_attribute_present"])
        self.assertEqual(link["href"]["query_parameter_names"], ["token"])
        self.assertNotIn("SECRET123", repr(report))
        initial = next(s for s in report["scripts"] if s["id"] == "initials-script")
        self.assertIn("$.videoModel", repr(initial["json"]["interesting_key_paths"]))
        self.assertIn("video", report["media_elements"][0]["tag"])


    @patch("resolver.http_session")
    def test_source_fetch_uses_shared_cookie_session(self, shared_session):
        class FakeResponse:
            status_code = 200
            headers = {}
            encoding = "utf-8"
            url = "https://mobilexh.live/videos/example/"

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield b"<html><title>Example</title></html>"

            def close(self):
                return None

        session = shared_session.return_value
        session.get.return_value = FakeResponse()
        url, body = _read_source_page(
            "https://mobilexh.live/videos/example/",
            "mobilexh.live",
        )
        self.assertEqual(url, "https://mobilexh.live/videos/example/")
        self.assertIn("<title>Example</title>", body)
        shared_session.assert_called_once_with()
        session.get.assert_called_once()
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
