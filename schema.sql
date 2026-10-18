-- URL columns contain paths only; domains are supplied by application configuration.
CREATE TABLE IF NOT EXISTS extraction_categories (
 category_slug TEXT PRIMARY KEY,
 category_name TEXT NOT NULL,
 category_path TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','completed','failed')),
 next_page INTEGER NOT NULL DEFAULT 1 CHECK(next_page>=1),
 last_page INTEGER NOT NULL DEFAULT 0 CHECK(last_page>=0),
 max_pages INTEGER NOT NULL DEFAULT 40 CHECK(max_pages>=1),
 completion_reason TEXT NULL CHECK(completion_reason IN ('limit','end')),
 lease_owner TEXT NULL,
 lease_expires_at TIMESTAMPTZ NULL,
 attempts INTEGER NOT NULL DEFAULT 0,
 consecutive_failures INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_extraction_categories_claim
 ON extraction_categories(status,lease_expires_at,category_slug);

CREATE TABLE IF NOT EXISTS videos (
 id BIGINT PRIMARY KEY,
 title TEXT NOT NULL,
 page_path TEXT NOT NULL,
 duration_seconds INTEGER NULL,
 created_epoch BIGINT NULL,
 views BIGINT NULL,
 video_type TEXT NULL,
 thumb_id BIGINT NULL,
 thumbnail_path TEXT NULL,
 image_path TEXT NULL,
 preview_thumb_path TEXT NULL,
 sprite_path TEXT NULL,
 trailer_path TEXT NULL,
 trailer_fallback_path TEXT NULL,
 trailer_cdn_version SMALLINT NULL,
 access_status TEXT NOT NULL DEFAULT 'unknown'
   CHECK(access_status IN ('unknown','non_premium','premium','inaccessible')),
 first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_videos_views ON videos(views DESC);
CREATE INDEX IF NOT EXISTS idx_videos_created_epoch ON videos(created_epoch DESC);

CREATE TABLE IF NOT EXISTS video_categories (
 category_slug TEXT NOT NULL REFERENCES extraction_categories(category_slug) ON DELETE CASCADE,
 video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(category_slug,video_id)
);
CREATE INDEX IF NOT EXISTS idx_video_categories_video_id ON video_categories(video_id);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS trailer_cdn_version SMALLINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS access_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE extraction_categories ADD COLUMN IF NOT EXISTS completion_reason TEXT;
ALTER TABLE extraction_categories ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE extraction_categories ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE extraction_categories ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0;
