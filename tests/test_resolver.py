import unittest
from resolver import StreamResolutionError, extract_m3u8_url, source_page_url

class ResolverTests(unittest.TestCase):
    def test_source_path(self):
        self.assertEqual(source_page_url("/videos/example/","https://mobilexh.live"),"https://mobilexh.live/videos/example/")
    def test_reject_absolute_source_url(self):
        with self.assertRaises(StreamResolutionError): source_page_url("https://evil.test/videos/x","https://mobilexh.live")
    def test_reject_other_path(self):
        with self.assertRaises(StreamResolutionError): source_page_url("/categories/x","https://mobilexh.live")
    def test_extract_escaped_manifest(self):
        page=r'<script>src="https:\/\/cdn.cdnsolutions.media\/hls\/master.m3u8?token=abc\u0026v=1"</script>'
        self.assertEqual(extract_m3u8_url(page,"https://mobilexh.live/videos/x/",{"mobilexh.live","cdnsolutions.media"}),"https://cdn.cdnsolutions.media/hls/master.m3u8?token=abc&v=1")
    def test_reject_unallowlisted_host(self):
        with self.assertRaises(StreamResolutionError): extract_m3u8_url('<script src="https://evil.test/master.m3u8"></script>',"https://mobilexh.live/videos/x/",{"mobilexh.live"})
if __name__=="__main__": unittest.main()
