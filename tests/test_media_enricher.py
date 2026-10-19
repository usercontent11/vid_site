import unittest

from media_enricher import (
    MediaExtractionError,
    extract_media_from_html,
    is_high_resolution_image_url,
    normalize_media_url,
    trailer_cdn_version,
    video_directory,
)

VIDEO_ID = 1193895
REQUESTED_URL = "https://mobilexh.live/videos/1193895"
PRIMARY_PAGE = "https://mobilexh.live/videos/ex-girl-friend-blowjob-xhRegression"

THUMB = (
    "https://ic-vt-nss.xhcdn.com/a/ThumbToken10/"
    "s(w:526,h:298),webp/001/193/895/526x298.10.webp"
)
IMAGE = (
    "https://ic-vt-nss.cdnsolutions.media/a/"
    "YWY4Y2RhNzQ4ZTY4ZDYyOGI5MTNlYTJiYjNlOWJmYTE/"
    "s(w:1280,h:720),webp/001/193/895/1280x720.10.jpg"
)
PREVIEW = (
    "https://thumb-nss.xhcdn.com/a/98FUYah4CwOMwgXm_08fcw/"
    "001/193/895/526x298.78.t.mp4"
)
SPRITE = (
    "https://thumb-v5.xhcdn.com/a/1CEYcCbBHCk8lcuqtxcXrw/"
    "001/193/895/160x160.50.s.jpg"
)
TRAILER = (
    "https://thumb-v9.cdnsolutions.media/a/TrailerToken9/"
    "001/193/895/526x298.94.3.2.t.av1.mp4"
)
FALLBACK = (
    "https://thumb-v5.xhcdn.com/a/FallbackToken5/"
    "001/193/895/526x298.94.3.2.t.mp4"
)


def page_html(
    video_id=VIDEO_ID,
    include_same_page_card=True,
    include_recommendation=True,
    primary_media=True,
    high_resolution_thumb=False,
):
    model_thumb = IMAGE if high_resolution_thumb else THUMB
    model = {
        "id": video_id,
        "modelName": "videoModel",
        "idHashSlug": "xhRegression",
        "pageURL": PRIMARY_PAGE,
        "title": "Regression fixture",
        "thumbURL": model_thumb if primary_media else None,
        "previewThumbURL": PREVIEW if primary_media else None,
        "spriteURL": SPRITE if primary_media else None,
        "trailerURL": TRAILER if primary_media else None,
        "trailerFallbackUrl": FALLBACK if primary_media else None,
    }
    entity = {
        "id": video_id,
        "thumbs": {"10": THUMB} if primary_media else {},
        "thumbBig": THUMB if primary_media else None,
        "capturedFrames": [{"captureTime": 0, "imgFrameSrc": THUMB}] if primary_media else [],
    }
    cards = []
    if include_same_page_card:
        cards.append({
            "id": VIDEO_ID,
            "videoType": "video",
            "title": "Regression fixture",
            "pageURL": PRIMARY_PAGE,
            "thumbURL": THUMB,
            "imageURL": IMAGE,
            "previewThumbURL": PREVIEW,
            "spriteURL": SPRITE,
            "trailerURL": TRAILER,
            "trailerFallbackUrl": FALLBACK,
            "thumbId": 10,
        })
    if include_recommendation:
        cards.append({
            "id": 987654321,
            "videoType": "video",
            "title": "A different recommended video",
            "pageURL": "https://mobilexh.live/videos/a-different-video",
            "thumbURL": "https://ic-vt-nss.xhcdn.com/a/wrongThumb/987/654/321/526x298.41.webp",
            "imageURL": "https://ic-vt-nss.xhcdn.com/a/wrongImage/987/654/321/1280x720.41.jpg",
            "previewThumbURL": "https://thumb-nss.xhcdn.com/a/wrongPreview/987/654/321/16x9.41.webp",
            "spriteURL": "https://thumb-v5.xhcdn.com/a/wrongSprite/987/654/321/526x298.s.webp",
            "trailerURL": "https://thumb-v5.xhcdn.com/a/wrongTrailer/987/654/321/526x298.94.t.mp4",
            "trailerFallbackUrl": "https://thumb-v9.xhcdn.com/a/wrongFallback/987/654/321/526x298.94.t.mp4",
            "thumbId": 41,
        })
    state = {
        "videoModel": model,
        "videoEntity": entity,
        "relatedVideosComponent": {
            "videoId": video_id,
            "videoTabInitialData": {
                "videoListProps": {"videoThumbProps": cards}
            },
        },
    }
    import json
    return (
        "<html><head><script id='initials-script'>window.initials="
        + json.dumps(state)
        + ";</script></head><body></body></html>"
    )


class MediaEnricherTests(unittest.TestCase):
    def test_video_directory_for_regression_id(self):
        self.assertEqual(video_directory(1193895), "001/193/895")

    def test_video_directory_refuses_unverified_long_ids(self):
        with self.assertRaisesRegex(ValueError, "exceeds the observed nine-digit"):
            video_directory(1_234_567_890)

    def test_extracts_primary_video_fields_and_normalizes_each_path(self):
        result = extract_media_from_html(page_html(), VIDEO_ID, REQUESTED_URL, existing_thumb_id=10)
        self.assertEqual(result.primary_model_id, VIDEO_ID)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.fields["thumbnail_path"], "/a/ThumbToken10/s(w:526,h:298),webp/001/193/895/526x298.10.webp")
        self.assertEqual(result.fields["image_path"], "/a/YWY4Y2RhNzQ4ZTY4ZDYyOGI5MTNlYTJiYjNlOWJmYTE/s(w:1280,h:720),webp/001/193/895/1280x720.10.jpg")
        self.assertEqual(result.fields["preview_thumb_path"], "/a/98FUYah4CwOMwgXm_08fcw/001/193/895/526x298.78.t.mp4")
        self.assertEqual(result.fields["sprite_path"], "/a/1CEYcCbBHCk8lcuqtxcXrw/001/193/895/160x160.50.s.jpg")
        self.assertEqual(result.fields["trailer_path"], "/a/TrailerToken9/001/193/895/526x298.94.3.2.t.av1.mp4")
        self.assertEqual(result.fields["trailer_fallback_path"], "/a/FallbackToken5/001/193/895/526x298.94.3.2.t.mp4")
        self.assertEqual(result.fields["thumb_id"], 10)
        self.assertEqual(result.fields["trailer_cdn_version"], 9)
        self.assertEqual(result.hosts["image_path"], "ic-vt-nss.cdnsolutions.media")
        self.assertEqual(result.hosts["sprite_path"], "thumb-v5.xhcdn.com")
        self.assertEqual(result.hosts["trailer_path"], "thumb-v9.cdnsolutions.media")
        self.assertEqual(result.source_keys["image_path"], "same_page_videoThumbProps.imageURL")

    def test_video_model_high_res_thumb_becomes_image_not_low_res_thumbnail(self):
        result = extract_media_from_html(
            page_html(include_same_page_card=False, high_resolution_thumb=True),
            VIDEO_ID,
            REQUESTED_URL,
            existing_thumb_id=10,
        )
        self.assertEqual(result.fields["image_path"], "/a/YWY4Y2RhNzQ4ZTY4ZDYyOGI5MTNlYTJiYjNlOWJmYTE/s(w:1280,h:720),webp/001/193/895/1280x720.10.jpg")
        self.assertEqual(result.fields["thumbnail_path"], "/a/ThumbToken10/s(w:526,h:298),webp/001/193/895/526x298.10.webp")
        self.assertEqual(result.source_keys["image_path"], "videoModel.thumbURL (high-resolution variant)")

    def test_recommendations_are_never_used_for_the_primary_video(self):
        result = extract_media_from_html(
            page_html(include_same_page_card=False, primary_media=False),
            VIDEO_ID,
            REQUESTED_URL,
        )
        self.assertEqual(result.status, "no_media")
        self.assertIsNone(result.fields["image_path"])
        self.assertIsNone(result.fields["sprite_path"])
        self.assertIsNone(result.fields["trailer_path"])
        self.assertNotIn("wrong", str(result.fields))
        self.assertNotIn("wrongImage", str(result.source_keys))

    def test_missing_primary_model_reports_initial_state_shape(self):
        import json

        state = {"videoEntity": {"id": VIDEO_ID}, "pagesCategoryComponent": {}}
        body = (
            "<html><script id='initials-script'>window.initials="
            + json.dumps(state)
            + ";</script></html>"
        )
        with self.assertRaises(MediaExtractionError) as caught:
            extract_media_from_html(body, VIDEO_ID, REQUESTED_URL)
        self.assertEqual(caught.exception.code, "primary_model_missing")
        self.assertIn("initials_keys=", str(caught.exception))
        self.assertIn("videoEntity", str(caught.exception))
        self.assertIn("known_key_types=", str(caught.exception))

    def test_missing_primary_model_with_verification_marker_is_terminal(self):
        import json

        body = (
            "<html><body>Verify your age to continue</body>"
            "<script id='initials-script'>window.initials="
            + json.dumps({"videoEntity": {"id": VIDEO_ID}})
            + ";</script></html>"
        )
        with self.assertRaises(MediaExtractionError) as caught:
            extract_media_from_html(body, VIDEO_ID, REQUESTED_URL)
        self.assertEqual(caught.exception.code, "source_restricted")
        self.assertTrue(caught.exception.terminal)
        self.assertIn("verify your age", str(caught.exception))

    def test_primary_model_id_must_match_database_id(self):
        with self.assertRaisesRegex(MediaExtractionError, "does not match requested database id"):
            extract_media_from_html(
                page_html(video_id=12345678),
                VIDEO_ID,
                REQUESTED_URL,
            )

    def test_url_normalization_preserves_query_and_drops_only_fragment(self):
        path, host = normalize_media_url(
            "https://thumb-v5.xhcdn.com/a/TokenABC/001/193/895/16x9.10.webp?token=a%2Fb&v=2#client-fragment",
            VIDEO_ID,
            "thumbnail_path",
        )
        self.assertEqual(path, "/a/TokenABC/001/193/895/16x9.10.webp?token=a%2Fb&v=2")
        self.assertEqual(host, "thumb-v5.xhcdn.com")

    def test_rejects_wrong_video_directory_and_untrusted_host(self):
        with self.assertRaisesRegex(ValueError, "does not contain the requested video's directory"):
            normalize_media_url(
                "https://thumb-v5.xhcdn.com/a/TokenABC/987/654/321/16x9.10.webp",
                VIDEO_ID,
                "thumbnail_path",
            )
        with self.assertRaisesRegex(ValueError, "unexpected media hostname"):
            normalize_media_url(
                "https://evil.example/a/TokenABC/001/193/895/16x9.10.webp",
                VIDEO_ID,
                "thumbnail_path",
            )

    def test_shard_is_taken_from_trailer_host_only(self):
        self.assertEqual(trailer_cdn_version("thumb-v9.cdnsolutions.media"), 9)
        self.assertEqual(trailer_cdn_version("thumb-v5.xhcdn.com"), 5)
        self.assertIsNone(trailer_cdn_version("thumb-nss.xhcdn.com"))
        self.assertTrue(is_high_resolution_image_url(IMAGE))
        self.assertFalse(is_high_resolution_image_url(THUMB))


if __name__ == "__main__":
    unittest.main()
