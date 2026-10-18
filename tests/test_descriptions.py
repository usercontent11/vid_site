import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from extractor import SourceFormatError
from description_extractor import (
    extract_initials_script_source,
    extract_video_description,
    make_video_url,
    save_initials_script_diagnostic,
)


def html_with_initials(payload):
    return (
        '<html><script id="initials-script">'
        'window.initials = ' + json.dumps(payload) + ';'
        '</script></html>'
    )


class DescriptionExtractorTests(unittest.TestCase):
    def test_reads_video_model_description_not_player_accessibility_field(self):
        html = html_with_initials({
            "videoModel": {
                "description": "The actual video description.",
                "videoDescription": "Accessibility instructions for player controls.",
            }
        })
        self.assertEqual(extract_video_description(html), "The actual video description.")

    def test_does_not_fall_back_to_video_description(self):
        html = html_with_initials({
            "videoModel": {
                "videoDescription": "Player accessibility instructions only."
            }
        })
        with self.assertRaisesRegex(SourceFormatError, "videoModel.description is missing"):
            extract_video_description(html)

    def test_null_description_is_distinct_from_missing_field(self):
        html = html_with_initials({"videoModel": {"description": None}})
        self.assertIsNone(extract_video_description(html))

    def test_rejects_non_string_description(self):
        html = html_with_initials({"videoModel": {"description": {"text": "not a string"}}})
        with self.assertRaisesRegex(SourceFormatError, "must be a string or null"):
            extract_video_description(html)


    def test_diagnostic_saves_exact_initials_script_and_metadata(self):
        script = '\\nwindow.initials = {"unexpectedPageModel": {"ok": true}};\\n'
        html = '<html><script id="initials-script">' + script + '</script></html>'
        self.assertEqual(extract_initials_script_source(html), script)

        video = {"id": 12345, "title": "Diagnostic test", "page_path": "/videos/diagnostic-test"}
        with TemporaryDirectory() as tmp:
            saved = save_initials_script_diagnostic(
                video, script, SourceFormatError("videoModel missing"), diagnostic_dir=tmp
            )
            self.assertEqual(saved.name, "video-12345-initials-script.js")
            self.assertEqual(saved.read_text(encoding="utf-8"), script)
            metadata = json.loads((Path(tmp) / "video-12345-diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["video_id"], 12345)
            self.assertEqual(metadata["error"], "videoModel missing")
            self.assertEqual(metadata["script_file"], saved.name)

    def test_diagnostic_script_extractor_returns_none_if_script_is_absent(self):
        self.assertIsNone(extract_initials_script_source("<html>no initials script</html>"))

    def test_only_builds_same_site_video_paths(self):
        self.assertTrue(make_video_url("/videos/example").endswith("/videos/example"))
        for bad_path in (
            "https://attacker.example/videos/a",
            "//attacker.example/videos/a",
            "/videos/a?next=https://attacker.example",
            "/categories/example",
        ):
            with self.subTest(path=bad_path), self.assertRaises(ValueError):
                make_video_url(bad_path)


if __name__ == "__main__":
    unittest.main()
