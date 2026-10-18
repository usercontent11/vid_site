import unittest

from resolver import StreamResolutionError, extract_m3u8_url, source_page_url


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


if __name__ == "__main__":
    unittest.main()
