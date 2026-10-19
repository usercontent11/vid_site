import unittest
from unittest.mock import Mock, patch

from resolver import (StreamResolutionError, extract_m3u8_url, extract_download_file_url,
                      resolve_stream_source, source_page_url, _read_source_page)


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


    def test_appcontext_javascript_report_exposes_property_names_only(self):
        from resolver import inspect_page_structure

        page = """<script id="appcontext-script">
        window.appContext = { playerConfig: { media: { manifestUrl: "https://secret.example/master?token=SECRET" } },
        videoId: 12, controls: true };
        </script>"""
        report = inspect_page_structure(page, "https://mobilexh.live/videos/test/")
        script = next(s for s in report["scripts"] if s["id"] == "appcontext-script")
        shape = script["javascript_structure"]
        self.assertIn("manifestUrl", shape["property_names"])
        self.assertIn("playerConfig", shape["player_related_property_names"])
        self.assertFalse(shape["values_returned"])
        self.assertNotIn("SECRET", repr(shape))
        self.assertNotIn("secret.example", repr(shape))


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


    def test_extracts_same_site_video_model_download_file(self):
        page = '''<script id="initials-script">window.initials = {"videoModel":{"downloadFile":"/movies/example/download/144p"}};</script>'''
        self.assertEqual(
            extract_download_file_url(page, "https://mobilexh.live/videos/example/"),
            "https://mobilexh.live/movies/example/download/144p",
        )

    def test_rejects_third_party_video_model_download_file(self):
        page = '''<script id="initials-script">window.initials = {"videoModel":{"downloadFile":"https://evil.example/movies/example/download/144p"}};</script>'''
        self.assertIsNone(
            extract_download_file_url(page, "https://mobilexh.live/videos/example/")
        )

    @patch("resolver._read_source_page")
    def test_stream_source_prefers_hls_over_download_file(self, read_page):
        page = '''<link rel="preload" as="fetch" crossorigin href="https://media.example-cdn.net/hls/master?sig=abc">
        <script id="initials-script">window.initials = {"videoModel":{"downloadFile":"/movies/example/download/144p"}};</script>'''
        read_page.return_value = ("https://mobilexh.live/videos/example/", page)
        self.assertEqual(
            resolve_stream_source("/videos/example/", "https://mobilexh.live"),
            ("https://media.example-cdn.net/hls/master?sig=abc", "hls"),
        )

    @patch("resolver._read_source_page")
    def test_stream_source_falls_back_to_same_site_download_file(self, read_page):
        page = '''<script id="initials-script">window.initials = {"videoModel":{"downloadFile":"/movies/example/download/144p"}};</script>'''
        read_page.return_value = ("https://mobilexh.live/videos/example/", page)
        self.assertEqual(
            resolve_stream_source("/videos/example/", "https://mobilexh.live"),
            ("https://mobilexh.live/movies/example/download/144p", "file"),
        )


    def test_no_manifest_error_reports_model_state_without_stream_urls(self):
        page = '''<title>xHamster</title><script id="initials-script">window.initials = {
          "avPlaceholderComponent":{"title":"To watch this video, you must<br>verify your age and be logged in"},
          "downloadDropdownComponent":{"sources":{"mp4":{"144p":"https://media.example/secret.mp4?token=secret"}}}
        };</script>'''
        with self.assertRaises(StreamResolutionError) as raised:
            extract_m3u8_url(page, "https://mobilexh.live/videos/example/")
        message = str(raised.exception)
        self.assertIn("videoModel_present=False", message)
        self.assertIn("videoModel_type=missing", message)
        self.assertIn("age_verification_prompt=True", message)
        self.assertIn("source_type_keys=['mp4']", message)
        self.assertNotIn("secret.mp4", message)
        self.assertNotIn("token=secret", message)


    @patch("resolver.requests.get")
    def test_neon_extractor_prefers_hls_and_rewrites_video_nss_domain(self, mock_get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "ok": True,
            "links": [
                "https://thumb-v5.xhcdn.com/a/preview/526x298.78.t.mp4",
                "https://video7.xhcdn.com/key=xyz/001/193/895/240p.h264.mp4",
                "https://video-nss.xhcdn.com/token/media=hls4/001/193/895/_TPL_.h264.mp4.m3u8",
            ],
        }
        mock_get.return_value = response
        with patch.dict("os.environ", {"NEON_VIDEO_EXTRACTOR_URL": "https://debug.example/"}):
            result = resolve_stream_source("/videos/example/", "https://mobilexh.live")
        self.assertEqual(
            result,
            ("https://video-nss.cdnsolutions.media/token/media=hls4/001/193/895/_TPL_.h264.mp4.m3u8", "hls"),
        )
        self.assertEqual(
            mock_get.call_args.kwargs["params"]["url"],
            "https://mobilexh.live/videos/example/",
        )

    @patch("resolver.requests.get")
    def test_neon_extractor_uses_full_mp4_not_thumbnail_when_no_hls(self, mock_get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "ok": True,
            "links": [
                "https://thumb-v5.xhcdn.com/a/preview/526x298.78.t.mp4",
                "https://video7.xhcdn.com/key=xyz/001/193/895/240p.h264.mp4",
            ],
        }
        mock_get.return_value = response
        with patch.dict("os.environ", {"NEON_VIDEO_EXTRACTOR_URL": "https://debug.example/"}):
            result = resolve_stream_source("/videos/example/", "https://mobilexh.live")
        self.assertEqual(result, (
            "https://video7.xhcdn.com/key=xyz/001/193/895/240p.h264.mp4", "file"
        ))


if __name__ == "__main__":
    unittest.main()
