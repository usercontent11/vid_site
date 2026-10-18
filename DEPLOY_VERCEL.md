# Deploy the Flask video site to Vercel

Import this repository into Vercel and set the project root to the repository root. The Flask app is exported as `app` from `app.py`.

## Required environment variables

- `DATABASE_URL`: Neon PostgreSQL connection string with SSL enabled.
- `SITE_URL`: the production website origin, such as `https://your-domain.example`.
- `SITE_NAME`: display name for page titles and the site header.

Optional values:
- `SOURCE_BASE_URL`: source domain, defaults to `https://mobilexh.live`.
- `ASSET_BASE_URL`: thumbnail CDN, defaults to `https://ic-vt-nss.cdnsolutions.media`.
- `PER_PAGE`: videos per page, defaults to 30 and is capped at 60.
- `SOURCE_COOKIE`: when the source page needs the extra preference/state cookie, set this to the raw `g_state` JSON value (starting with `{` and ending with `}`, without a `g_state=` prefix). Vercel does not inherit GitHub Actions secrets, so add it separately in Vercel. Keep the value secret.

## Features

The Flask/Jinja site includes the home feed, category directory and category pages, search, individual watch pages, responsive thumbnail cards, paginated navigation, related videos from shared category membership, canonical URLs, page metadata, JSON-LD, `robots.txt`, and batched XML sitemaps.

## How stream resolution works

When a video page opens, the player automatically calls `/api/stream/<video_id>`. Flask reads the stored `page_path` from Neon, combines it with `SOURCE_BASE_URL`, requests that page, extracts an HTTPS M3U8 manifest, and returns it directly to the browser. The M3U8 URL is not stored in Neon or in a file, and there is no stream cache table. Every page load resolves the stream again.

The resolver reuses the source session and its configured cookies. Logs report whether `g_state` is present without exposing cookie values. If the page responds with generic site data instead of video-player data, configure `SOURCE_COOKIE` in Vercel as described above. The resolver requests only the configured source domain and rejects redirects to a different host. The returned manifest must be an HTTPS `.m3u8` URL; the stream hostname can vary per video and does not need to be manually configured. Playback still depends on the media host allowing the browser to fetch its manifest and media segments cross-origin.

This HTTP request is implemented in Flask rather than in a regular PostgreSQL function. Some PostgreSQL HTTP extensions can enqueue asynchronous requests, but that is different from synchronously fetching a page and parsing its HTML to return an M3U8 in one database-function call. Doing it in Flask matches the on-page request/response flow and works with the site's Vercel deployment. The resolver rejects records marked `premium` or `inaccessible` and does not inject login cookies or bypass access controls.

## Deploy checklist

1. Add environment variables in Vercel's Project Settings. Never commit your real database URL.
2. Deploy, then test `/healthz`, `/robots.txt`, `/sitemap.xml`, a category page, and a video page.
3. Open a video page and verify the Network panel shows a successful `/api/stream/<video_id>` response. If the response says the M3U8 was not found, the source may put the stream data in a format the current extractor does not yet recognize.
4. Test playback on your actual domain. If the M3U8 URL is returned but media fails, the stream host may block cross-origin browser playback or require headers the browser cannot send.
5. Submit `/sitemap.xml` in Google Search Console and validate video structured data with Google's Rich Results Test.

SEO markup improves crawlability and rich-result eligibility; it cannot guarantee ranking for every URL. Search engines still evaluate unique content, quality, page experience, playback availability, and site authority.
