import unittest

from scripts.capture_source_page import redact_html


class SourcePageCaptureTests(unittest.TestCase):
    def test_redacts_query_values_but_keeps_parameter_names(self):
        html = '<link href="https://cdn.example/master?token=SECRET&quality=high">'
        safe = redact_html(html)
        self.assertIn("token=[REDACTED]", safe)
        self.assertIn("quality=[REDACTED]", safe)
        self.assertNotIn("SECRET", safe)
        self.assertNotIn("high", safe)

    def test_redacts_credential_assignments(self):
        html = '''<script>window.data = {"stats_tkn":"PRIVATE","title":"example"};</script>'''
        safe = redact_html(html)
        self.assertNotIn("PRIVATE", safe)
        self.assertIn('"title":"example"', safe)
        self.assertIn("[REDACTED]", safe)

    def test_redacts_json_unicode_escaped_query_parameters(self):
        html = r'''<script>var url="https://cdn.example/master?token\u003dSECRET\u0026sig\u003dSIGNATURE";</script>'''
        safe = redact_html(html)
        self.assertNotIn("SECRET", safe)
        self.assertNotIn("SIGNATURE", safe)


if __name__ == "__main__":
    unittest.main()
