# Deploy the Flask video site to Vercel

## Configuration

Import this repository into Vercel and keep the project root at the repository root. Vercel detects the Flask application exported as \`app\` in \`app.py\`. Set the following project Environment Variables:

- \`DATABASE_URL\`: Neon PostgreSQL connection string, with SSL enabled.
- \`SITE_URL\`: the production site's canonical origin, such as \`https://your-domain.example\`.
- \`SITE_NAME\`: the name displayed in the header and title tags.
- \`SOURCE_BASE_URL\`: defaults to \`https://mobilexh.live\`.
- \`ASSET_BASE_URL\`: defaults to \`https://ic-vt-nss.cdnsolutions.media\`.
- \`PER_PAGE\`: defaults to 30 videos per page.
- \`STREAM_ALLOWED_HOSTS\`: comma-separated verified hostnames for external M3U8 manifests. The source host is automatically trusted and \`cdnsolutions.media\` is the default CDN allowlist.

Deploy and test \`/healthz\`, \`/robots.txt\`, \`/sitemap.xml\`, a category page, and an individual video page. Submit \`/sitemap.xml\` in Google Search Console after the custom domain is connected. Use Google's Rich Results Test to validate the video pages.

## Features

- Server-rendered Flask/Jinja pages for the home feed, category directory, category listings, search, and individual video watch pages.
- 30 videos per page by default, crawlable pagination links, canonical URLs, responsive thumbnail cards, video metadata, category chips, and related videos selected from shared category membership.
- JSON-LD for Website, BreadcrumbList, and VideoObject only when a usable thumbnail and source upload timestamp exist. Missing descriptions and thumbnails fall back gracefully; no made-up upload date is inserted into VideoObject data.
- Split XML sitemap index with static/category and batched video sitemaps, robots.txt, and a basic health check.
- Short-lived HLS URL caching in Neon and a player UI using native HLS or hls.js.

## Playback resolver and database behavior

The Load video button calls \`/api/stream/<video_id>\`. Flask builds the source URL from the row's path-only \`page_path\`, requests only the configured source host, extracts an HTTPS M3U8 manifest, validates the manifest's hostname, and stores the result in the Neon table \`video_stream_cache\` for a short TTL. This cache table is automatically created on the first playback request. If the actual M3U8 host differs from \`mobilexh.live\` or \`cdnsolutions.media\`, add its verified hostname to \`STREAM_ALLOWED_HOSTS\`. Playback also requires the media host to permit browser cross-origin HLS requests.

A normal Neon PostgreSQL function does not make arbitrary outbound HTTP requests by itself. The HTTP call therefore lives in the Flask backend rather than in PL/pgSQL; Neon holds the short-lived cache. The resolver rejects rows marked \`premium\` or \`inaccessible\`, never accepts arbitrary user-supplied URLs, refuses cross-host redirects, and does not inject login cookies or attempt to defeat access controls.

## SEO expectations

The site returns indexable server-rendered HTML and includes canonical URLs, titles/descriptions, internal category and related-video links, structured data, and sitemaps. These help discovery and eligibility; they cannot guarantee that every page will rank. Pages still need useful, distinctive metadata, accessible playback, quality content, good performance, and ongoing review in Search Console.
