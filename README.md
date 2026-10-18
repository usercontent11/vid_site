# Category video metadata extractor

This workflow extracts category cards into Neon/PostgreSQL. It creates/migrates tables automatically and saves video rows plus pagination progress in the same transaction.

## GitHub Actions configuration

- Add `DATABASE_URL` as a repository Actions secret (Neon connection string).
- `SITE_BASE_URL` is optional as a repository variable or secret; the default is `https://mobilexh.live`.
- The request session loads the supplied age/preference cookies by default. The `g_state` value is not embedded in public source. `SOURCE_COOKIE` may contain a Netscape cookie export, a conventional Cookie header, or the raw JSON value of the `g_state` cookie (with `i_l`, `i_ll`, `i_b`, `i_e`, and `i_et` fields). A raw `g_state` JSON value is added alongside the default preference cookies; a complete Netscape export or Cookie header replaces the defaults. Cookie values are not logged.

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
