# Category video metadata extractor

This workflow extracts category cards into Neon/PostgreSQL. It creates/migrates tables automatically and saves video rows plus pagination progress in the same transaction.

## GitHub Actions configuration

- Add `DATABASE_URL` as a repository Actions secret (Neon connection string).
- `SITE_BASE_URL` is optional as a repository variable or secret; the default is `https://mobilexh.live`.
- The request session loads the supplied age/preference cookies by default. The `g_state` value is not embedded in public source. `SOURCE_COOKIE` may contain a Netscape cookie export, a conventional Cookie header, or the raw JSON value of the `g_state` cookie (with `i_l`, `i_ll`, `i_b`, `i_e`, and `i_et` fields). A raw `g_state` JSON value is added alongside the default preference cookies; a complete Netscape export or Cookie header replaces the defaults. Cookie values are not logged.

## Source page structure capture

Open Actions → **Inspect source video page** → Run workflow. The default path is /videos/xhfKO6H; change `page_path` to another stored /videos/... path to inspect a different page. The workflow uses `SITE_BASE_URL` (default https://mobilexh.live) and the optional `SOURCE_COOKIE` Actions secret.

On success, the workflow saves and commits two files under `diagnostics/`:
- `source-page-response.html`: a readable copy of the response with all URL query values and credential/session-like values redacted.
- `source-page-structure.json`: script IDs and sizes, link/media element summaries, JSON key paths, and inline app-context property names, without returning embedded values.

This is a sanitized snapshot, not the byte-for-byte raw response. The purpose is to inspect markup and script structure without committing signed URLs, tokens, or cookie values to this public repository. The workflow needs repository contents: write permission to commit the files. Each run overwrites the previous snapshot, so inspect or save a copy before running it for another page.

## Video playback resolution

The stream endpoint first uses a detected HLS manifest. If the page has no usable HLS URL, it may fall back to `videoModel.downloadFile` only when the URL points to the source site's own HTTPS `/movies/.../download/...` route. The API returns a `type` of `hls` or `file`, and the browser uses HLS.js only for HLS manifests. Existing premium/inaccessible database rows remain unavailable for playback, and resolved media URLs are not persisted.

## Test source-page access from a Neon Function

The **Probe source page from Neon Function** workflow deploys a protected Node.js 24 function to a selected Neon branch, fetches a source video page from Neon’s network, and commits a redacted structure report at `diagnostics/neon-source-page-structure.json`. This probe comes before moving the live resolver: we first compare what Neon receives against the GitHub Actions and Vercel responses.

Configure these GitHub Actions repository secrets:
- `NEON_API_KEY`: a Neon API key allowed to deploy Functions.
- `NEON_PROJECT_ID`: the Neon project ID.
- `NEON_BRANCH_ID`: the branch ID where the Function should be deployed.
- `NEON_INSPECT_TOKEN`: a new long random secret used to protect the inspection endpoint.
- `SOURCE_COOKIE`: optional. Supply the existing source cookie configuration only if the default preference cookies return a different page.

`SITE_BASE_URL` is an optional repository variable; its default is `https://mobilexh.live`. Open Actions → **Probe source page from Neon Function** → Run workflow. The default page is `/videos/xhfKO6H`; choose another path beginning with `/videos/` to test a different page. Never commit the inspection token or source cookies.

The function returns only response status/size, script and link structure, JSON key names, and safe URL summaries (scheme, host, path, and query-parameter names). It does not return raw HTML, cookie values, or query values. The endpoint requires the bearer token and rejects paths outside the configured source's `/videos/` route. After this diagnostic confirms Neon gets the expected page shape, the production stream resolver can be moved over separately.

## Category extraction run

Open Actions → Extract category video metadata → Run workflow.

- `max_pages` defaults to 40 pages per category, and accepts 1-2000.
- `workers` defaults to 4 concurrent category workers.
- `retry_failed=true` requeues categories that exhausted retry attempts.

Page 1 is `/categories/erotica`; page 2 is `/categories/erotica/2`, etc. Page and asset URL columns store path only; source and asset/CDN domains are not stored. Rebuild full URLs using the application environment and CDN configuration. The observed `thumb-vN` asset CDN version is stored as an integer.

## Video description test phase

The separate **Test video descriptions** workflow uses direct asynchronous HTTP GET requests; it does not launch a browser or render pages. It fetches a small sample of existing rows with a NULL `description`, prioritizing videos with more views, and reads **only** `window.initials.videoModel.description`. The `videoDescription` field is intentionally ignored because it contains video-player accessibility instructions, not the actual description.

- The default sample size is 5 and the maximum is hard-capped at 50 per run.
- The default concurrency is 3 requests; the maximum is hard-capped at 8.
- Successful strings are saved into `videos.description` with an ID-based PostgreSQL upsert. Failed or unparseable pages do not overwrite existing descriptions.
- URL requests are built from the stored `page_path` and restricted to same-site `/videos/...` paths. Existing cookie configuration is reused, and temporary HTTP errors are retried.
- The workflow prints short description previews and a result count so you can inspect the test run. It does not send descriptions to an AI model.

Run this workflow first with the default **limit=5**. Inspect the Actions log and the corresponding rows in Neon before increasing the test sample. This workflow is purposefully bounded; it is not the bulk backfill job for all videos.

## Full-scale description backfill

After the small test reports successful extractions, open Actions → **Backfill video descriptions** → Run workflow. Defaults are a database `batch_size=100`, `workers=5`, and `max_videos=0` (all remaining eligible videos). The worker count is capped at 8 and the batch size at 500. A single run is serialized against other backfill runs.

The backfill iterates through existing `videos` rows with `description IS NULL`, using ID-ordered batches and direct asynchronous HTTP requests. Successful results are upserted into `videos.description`; rows that already have descriptions are skipped. Failed rows remain NULL and are eligible for another run, while successful rows are not refetched. The log reports starting counts, each batch, outcomes, and how many descriptions remain. Rows whose `page_path` is not a `/videos/...` path are not requested. Diagnostic initial-script files are uploaded as a separate artifact for 3 days if parsing fails.

For the first full run, keep the defaults and `max_videos=0`. If you prefer a bounded rollout, set `max_videos=500`; later runs automatically skip successfully populated descriptions and continue on the still-missing records. Do not run multiple backfills concurrently.

When a fetched page has a parse error, the workflow writes the exact inner contents of its `initials-script` element to `diagnostic/video-<id>-initials-script.js`, with a JSON sidecar describing the video ID and parse error. These files are **not committed to the repository**; a separate GitHub Actions artifact named `description-initial-script-diagnostics-<run id>` is uploaded for 3 days. No diagnostic script is written to Neon, and successful video rows keep the normal upsert behavior. The captured script may contain page-specific data, so download or share it only with trusted collaborators.

## Resume and cross-run coordination

Every category-extraction run using the same Neon database shares `extraction_categories`. A `FOR UPDATE SKIP LOCKED` claim, lease owner and expiry prevent two workers from intentionally claiming the same active category. Abandoned claims are recoverable after lease expiry. Per-page video upserts and cursor updates commit atomically. To extend from 40 pages to 80, dispatch with `max_pages=80`; categories stopped at the old limit resume from their stored `next_page`. Categories that reached a real end-of-list are not reopened automatically.

## Tables and fields

- `videos`: existing metadata fields plus nullable `description` and seen timestamps.
- `extraction_categories`: category path and group membership, cursor, cap, state, retries, error, and lease.
- `video_categories`: membership between videos and categories.

The video metadata fields are `id`, `title`, `page_path`, `description`, `duration_seconds`, `created_epoch`, `views`, `video_type`, `thumb_id`, `thumbnail_path`, `image_path`, `preview_thumb_path`, `sprite_path`, `trailer_path`, `trailer_fallback_path`, `trailer_cdn_version`, and `access_status`. The video ID is the primary key, so repeat sightings update metadata instead of adding duplicate rows.

## Category path overrides

`category_list.json` contains `groups`, each with `group` and `categories[]` entries containing `category` and a canonical `href`. The extractor uses each href's path directly instead of guessing from display names, and it rejects hrefs from a different host than SITE_BASE_URL. Duplicate hrefs across groups are deduplicated so the same listing isn't crawled twice; group names are retained in `extraction_categories.category_groups`. Category queue rows no longer present in the current JSON are disabled (not deleted), preventing entries from the previous catalog from being scraped. A bad redirect or empty page 1 is treated as an error.

## Premium status caveat

The supplied category-card data does not consistently expose a documented premium marker. The extractor skips a card only if it contains an explicit per-card premium marker. Otherwise its `access_status` stays `unknown` unless an explicit public marker exists. Unknown does not prove that access is free; later detail/API enrichment can refine it.

## Schema and reliability

The schema creates the tables automatically and includes idempotent migrations, including `ALTER TABLE videos ADD COLUMN IF NOT EXISTS description TEXT` for existing Neon databases. Schema initialization uses a PostgreSQL advisory lock. Category metadata extraction retries transient HTTP errors (429 and common 5xx); after five category errors the category is marked failed, and `retry_failed=true` can requeue it.
