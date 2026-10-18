# Category video metadata extractor

This workflow extracts category cards into Neon/PostgreSQL. It creates/migrates tables automatically and saves video rows plus pagination progress in the same transaction.

## GitHub Actions configuration

- Add `DATABASE_URL` as a repository Actions secret (Neon connection string).
- `SITE_BASE_URL` is optional as a repository variable or secret; the default is `https://mobilexh.live`.
- The request session loads the supplied age/preference cookies by default. The token-like `g_state` cookie is intentionally not embedded in public code. To use a refreshed full Netscape cookie export or conventional Cookie header, add it as the `SOURCE_COOKIE` Actions secret. Cookie values are not logged.

## Run

Open Actions → Extract category video metadata → Run workflow.

- `max_pages` defaults to 40 pages per category, and accepts 1-2000.
- `workers` defaults to 4 concurrent category workers.
- `retry_failed=true` requeues categories that exhausted retry attempts.

Page 1 is `/categories/erotica`; page 2 is `/categories/erotica/2`, etc. Page and asset URL columns store path only; source and asset/CDN domains are not stored. Rebuild full URLs using the application environment and CDN configuration. The observed `thumb-vN` asset CDN version is stored as an integer.

## Resume and cross-run coordination

Every run using the same Neon database shares `extraction_categories`. A `FOR UPDATE SKIP LOCKED` claim, lease owner and expiry prevent two workers from intentionally claiming the same active category. Abandoned claims are recoverable after lease expiry. Per-page video upserts and cursor updates commit atomically. To extend from 40 pages to 80, dispatch with `max_pages=80`; categories stopped at the old limit resume from their stored `next_page`. Categories that reached a real end-of-list are not reopened automatically.

## Tables and fields

- `videos`: 16 metadata fields plus seen timestamps.
- `extraction_categories`: category path, cursor, cap, state, retries, error, and lease.
- `video_categories`: membership between videos and categories.

The 16 metadata fields are `id`, `title`, `page_path`, `duration_seconds`, `created_epoch`, `views`, `video_type`, `thumb_id`, `thumbnail_path`, `image_path`, `preview_thumb_path`, `sprite_path`, `trailer_path`, `trailer_fallback_path`, `trailer_cdn_version`, `access_status`. The video ID is the primary key, so repeat sightings update metadata instead of adding duplicate rows.

## Category path overrides

`category_list.json` contains display names rather than canonical paths. Most names are slugified. For any category with a different actual source path, map its exact display name in `category_paths.json` to a path such as `/categories/canonical-slug`. Values must not include a domain. If a response redirects away from a category page or page 1 has no cards, the category fails rather than being falsely marked complete.

## Premium status caveat

The supplied category-card data does not consistently expose a documented premium marker. The extractor skips a card only if it contains an explicit per-card premium marker. Otherwise its `access_status` stays `unknown` unless an explicit public marker exists. Unknown does not prove that access is free; later detail/API enrichment can refine it.

## Extending the schema

Add new columns to the table definition and an idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration in `schema.sql`. Schema initialization uses a PostgreSQL advisory lock. Transient HTTP errors (429 and common 5xx) are retried; after five category errors the category is marked failed, and `retry_failed=true` can requeue it.
