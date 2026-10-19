#!/usr/bin/env python3
"""Validate primary-video media metadata and fill missing media columns in Neon.

This worker reuses extractor.py's PostgreSQL and source-cookie configuration. It
never downloads media files, changes category metadata, or rewrites access_status.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import aiohttp

from extractor import (
    DATABASE_URL,
    SITE_BASE_URL,
    TIMEOUT,
    SourceFormatError,
    db,
    http_session,
    parse_initials,
)

LOG = logging.getLogger("media-enricher")
MAX_HTML_BYTES = int(os.getenv("MEDIA_MAX_HTML_BYTES", str(3 * 1024 * 1024)))
HTTP_ATTEMPTS = 3
MAX_RECORD_ATTEMPTS = 5
MAX_REDIRECTS = 5
MEDIA_HOST_SUFFIXES = ("xhcdn.com", "cdnsolutions.media")
IMAGE_EXTENSIONS = {"apng", "avif", "bmp", "gif", "ico", "jpeg", "jpg", "png", "svg", "tif", "tiff", "webp"}
VIDEO_EXTENSIONS = {"m3u8", "mp4", "m4v", "mpd", "mov", "ts", "webm"}
URL_COLUMNS = (
    "thumbnail_path",
    "image_path",
    "preview_thumb_path",
    "sprite_path",
    "trailer_path",
    "trailer_fallback_path",
)
ALL_MEDIA_COLUMNS = URL_COLUMNS + ("thumb_id", "trailer_cdn_version")
COLUMN_CASTS = {
    "id": "BIGINT",
    "thumbnail_path": "TEXT",
    "image_path": "TEXT",
    "preview_thumb_path": "TEXT",
    "sprite_path": "TEXT",
    "trailer_path": "TEXT",
    "trailer_fallback_path": "TEXT",
    "thumb_id": "BIGINT",
    "trailer_cdn_version": "SMALLINT",
}

# Kept out of schema.sql intentionally: this optional sidecar is created only
# when the user explicitly enables writes (not by normal category extraction).
TRACKER_DDL = """
CREATE TABLE IF NOT EXISTS public.video_media_enrichment (
    video_id BIGINT PRIMARY KEY REFERENCES public.videos(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN
        ('complete','partial','no_media','inaccessible','retry','exhausted')),
    attempts SMALLINT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_http_status INTEGER,
    reason_code TEXT,
    last_error TEXT,
    missing_fields TEXT[] NOT NULL DEFAULT '{}'::text[],
    media_hosts JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_video_media_enrichment_retry
    ON public.video_media_enrichment(status, next_retry_at, video_id);
"""


class MediaExtractionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        terminal: bool = False,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.terminal = terminal
        self.http_status = http_status


@dataclass
class MediaResult:
    video_id: int
    page_url: str
    status: str = "partial"
    reason_code: str | None = None
    error: str | None = None
    http_status: int | None = None
    elapsed_seconds: float = 0.0
    primary_model_id: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    source_keys: dict[str, str] = field(default_factory=dict)
    hosts: dict[str, str] = field(default_factory=dict)
    rejected_fields: dict[str, str] = field(default_factory=dict)
    attempts_before: int = 0


def video_directory(video_id: int | str) -> str:
    """Return the observed 3-3-3 grouping, refusing IDs beyond nine digits."""
    value = int(video_id)
    if value < 0:
        raise ValueError("video ID must be a non-negative integer")
    digits = str(value)
    if len(digits) > 9:
        raise ValueError(
            f"video ID {value} exceeds the observed nine-digit media directory pattern; "
            "refusing to infer an unverified grouping"
        )
    padded = digits.zfill(9)
    return "/".join(padded[i:i + 3] for i in (0, 3, 6))


def _numeric_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _same_or_subdomain(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _is_source_host(host: str) -> bool:
    expected = (urlsplit(SITE_BASE_URL).hostname or "").lower()
    return bool(expected) and host.lower().rstrip(".") == expected


def _safe_video_url(page_path: Any) -> str:
    if not isinstance(page_path, str) or not page_path.strip():
        raise MediaExtractionError("invalid_page_path", "page_path is empty", terminal=True)
    parsed = urlsplit(page_path.strip())
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise MediaExtractionError(
            "invalid_page_path",
            "page_path must contain only a same-site /videos/... path",
            terminal=True,
        )
    if not parsed.path.startswith("/videos/") or parsed.path.startswith("//"):
        raise MediaExtractionError(
            "invalid_page_path", "page_path must begin with /videos/", terminal=True
        )
    if any(piece in {".", ".."} for piece in parsed.path.split("/")):
        raise MediaExtractionError("invalid_page_path", "page_path contains a traversal segment", terminal=True)
    base = SITE_BASE_URL.rstrip("/") + "/"
    url = urljoin(base, quote(parsed.path.lstrip("/"), safe="/%:@!$&'()*+,;=-._~"))
    target = urlsplit(url)
    if target.scheme != "https" or not _is_source_host(target.hostname or ""):
        raise MediaExtractionError("invalid_page_url", "page URL is not on configured HTTPS source host", terminal=True)
    return url


def _expected_directory_in_path(path: str, expected: str) -> bool:
    parts = [part for part in path.split("/") if part]
    wanted = expected.split("/")
    return any(parts[index:index + 3] == wanted for index in range(max(0, len(parts) - 2)))


def _path_extension(path: str) -> str:
    filename = path.rsplit("/", 1)[-1]
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def is_high_resolution_image_url(raw_url: Any) -> bool:
    if not isinstance(raw_url, str):
        return False
    try:
        path = urlsplit(html.unescape(raw_url.strip())).path.lower()
    except ValueError:
        return False
    return (
        "w:1280" in path
        or "w%3a1280" in path
        or "1280x720" in path
        or "2560x1440" in path
        or "/hq." in path
    )


def normalize_media_url(raw_url: Any, video_id: int, column: str) -> tuple[str, str]:
    """Validate a primary video's absolute asset URL; return path+query and hostname."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("URL is empty")
    value = html.unescape(raw_url.strip()).replace(r"\/", "/")
    value = re.sub(r"\\u002f", "/", value, flags=re.IGNORECASE)
    value = re.sub(r"\\u0026", "&", value, flags=re.IGNORECASE)
    value = re.sub(r"\\u003d", "=", value, flags=re.IGNORECASE)
    if value.startswith("//"):
        value = "https:" + value

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    if not any(_same_or_subdomain(host, domain) for domain in MEDIA_HOST_SUFFIXES):
        raise ValueError(f"unexpected media hostname {host!r}")
    if not parsed.path.startswith("/a/"):
        raise ValueError("video media path must start with /a/")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 3 or path_parts[0] != "a" or not path_parts[1]:
        raise ValueError("media URL is incomplete; expected /a/{token}/...")
    try:
        expected_directory = video_directory(video_id)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not _expected_directory_in_path(parsed.path, expected_directory):
        raise ValueError(
            f"media path does not contain the requested video's directory {expected_directory}"
        )

    extension = _path_extension(parsed.path)
    if column in {"thumbnail_path", "image_path", "sprite_path"}:
        if extension not in IMAGE_EXTENSIONS:
            raise ValueError(f"{column} must point to an image file, got .{extension or '(none)'}")
    elif column == "preview_thumb_path":
        if extension not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            raise ValueError(f"preview must point to an image or preview media file, got .{extension or '(none)'}")
    elif column in {"trailer_path", "trailer_fallback_path"}:
        if extension not in VIDEO_EXTENSIONS:
            raise ValueError(f"{column} must point to a video/media file, got .{extension or '(none)'}")

    # The database intentionally stores paths, but preserve encoded path/query bytes and case.
    path_only = urlunsplit(("", "", parsed.path, parsed.query, ""))
    if not path_only.startswith("/a/") or "/../" in path_only:
        raise ValueError("normalized media path is malformed")
    return path_only, host


def trailer_cdn_version(host: str | None) -> int | None:
    """Preserve only the numeric shard in the validated trailer's own hostname."""
    if not host:
        return None
    match = re.match(r"^thumb-v([0-9]+)\.", host.lower())
    if not match:
        return None
    version = int(match.group(1))
    return version if 0 <= version <= 32767 else None


def _same_canonical_video_page(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        a, b = urlsplit(left), urlsplit(right)
    except ValueError:
        return False
    if a.scheme != "https" or b.scheme != "https":
        return False
    if not _is_source_host(a.hostname or "") or not _is_source_host(b.hostname or ""):
        return False
    return a.path.rstrip("/") == b.path.rstrip("/")


def _known_video_card_lists(initials: dict[str, Any]) -> list[list[Any]]:
    """Return only observed video-card containers; never recursively search recommendations."""
    found: list[list[Any]] = []
    category = initials.get("pagesCategoryComponent")
    if isinstance(category, dict):
        trending = category.get("trendingVideoListProps")
        if isinstance(trending, dict) and isinstance(trending.get("videoThumbProps"), list):
            found.append(trending["videoThumbProps"])

    related = initials.get("relatedVideosComponent")
    if isinstance(related, dict):
        initial = related.get("videoTabInitialData")
        if isinstance(initial, dict):
            props = initial.get("videoListProps")
            if isinstance(props, dict) and isinstance(props.get("videoThumbProps"), list):
                found.append(props["videoThumbProps"])
    return found


def _find_same_page_card(initials: dict[str, Any], video_id: int, primary_page_url: str) -> dict[str, Any] | None:
    for cards in _known_video_card_lists(initials):
        for card in cards:
            if not isinstance(card, dict):
                continue
            if _numeric_id(card.get("id")) != video_id:
                continue
            if card.get("videoType") not in (None, "video"):
                continue
            if _same_canonical_video_page(card.get("pageURL"), primary_page_url):
                return card
    return None


def _asset_candidate(
    result: MediaResult,
    column: str,
    candidates: list[tuple[str, Any]],
    *,
    reject_high_res_for_thumbnail: bool = False,
) -> None:
    for source_key, raw in candidates:
        if raw is None or raw == "":
            continue
        if reject_high_res_for_thumbnail and is_high_resolution_image_url(raw):
            result.rejected_fields[column] = (
                f"{source_key} is a high-resolution image; it is not used as thumbnail_path"
            )
            continue
        try:
            normalized, host = normalize_media_url(raw, result.video_id, column)
        except (TypeError, ValueError) as exc:
            result.rejected_fields[column] = f"{source_key}: {exc}"
            continue
        result.fields[column] = normalized
        result.hosts[column] = host
        result.source_keys[column] = source_key
        return


def extract_media_from_html(
    body: str,
    video_id: int,
    page_url: str,
    existing_thumb_id: int | None = None,
) -> MediaResult:
    """Extract media only from a verified top-level videoModel / videoEntity."""
    try:
        video_directory(video_id)
    except ValueError as exc:
        raise MediaExtractionError("unsupported_video_id_layout", str(exc), terminal=True) from exc

    try:
        initials = parse_initials(body)
    except SourceFormatError as exc:
        snippet = body[:120_000].lower()
        challenge_markers = (
            "captcha", "challenge-platform", "checking your browser",
            "verify your age", "age verification", "must verify your age",
            "to watch this video, you must", "access denied", "attention required",
        )
        if any(marker in snippet for marker in challenge_markers):
            raise MediaExtractionError(
                "source_restricted",
                "The source returned a likely verification/challenge page; it was not bypassed",
                terminal=True,
            ) from exc
        raise MediaExtractionError("parse_failure", str(exc)) from exc

    model = initials.get("videoModel")
    if not isinstance(model, dict):
        raise MediaExtractionError("primary_model_missing", "window.initials.videoModel is missing or not an object")
    primary_id = _numeric_id(model.get("id"))
    if primary_id != int(video_id):
        raise MediaExtractionError(
            "id_mismatch",
            f"videoModel.id={model.get('id')!r} does not match requested database id {video_id}",
            terminal=True,
        )

    entity = initials.get("videoEntity")
    if isinstance(entity, dict):
        entity_id = _numeric_id(entity.get("id"))
        if entity_id is not None and entity_id != int(video_id):
            raise MediaExtractionError(
                "id_mismatch",
                f"videoEntity.id={entity.get('id')!r} does not match requested database id {video_id}",
                terminal=True,
            )

    related = initials.get("relatedVideosComponent")
    if isinstance(related, dict):
        related_id = _numeric_id(related.get("videoId"))
        if related_id is not None and related_id != int(video_id):
            raise MediaExtractionError(
                "id_mismatch",
                f"relatedVideosComponent.videoId={related.get('videoId')!r} does not match requested database id {video_id}",
                terminal=True,
            )

    primary_page_url = model.get("pageURL") or page_url
    try:
        canonical = urlsplit(str(primary_page_url))
    except ValueError as exc:
        raise MediaExtractionError("primary_page_url_invalid", "videoModel.pageURL is malformed", terminal=True) from exc
    if (
        canonical.scheme != "https"
        or not _is_source_host(canonical.hostname or "")
        or not canonical.path.startswith("/videos/")
        or canonical.username
        or canonical.password
    ):
        raise MediaExtractionError(
            "primary_page_url_invalid",
            "videoModel.pageURL is not a same-site HTTPS /videos/ URL",
            terminal=True,
        )

    same_page_card = _find_same_page_card(initials, int(video_id), str(primary_page_url))
    entity_thumbs = entity.get("thumbs") if isinstance(entity, dict) and isinstance(entity.get("thumbs"), dict) else {}
    result = MediaResult(
        video_id=int(video_id),
        page_url=str(primary_page_url),
        primary_model_id=primary_id,
        fields={column: None for column in ALL_MEDIA_COLUMNS},
    )

    # thumb_id is a structured key on category/video cards. Do not infer it from
    # a filename; an existing DB value may be used only to select videoEntity.thumbs.
    thumb_id_candidates = []
    if same_page_card is not None:
        thumb_id_candidates.append(("same_page_videoThumbProps.thumbId", same_page_card.get("thumbId")))
    thumb_id_candidates.extend([
        ("videoModel.thumbId", model.get("thumbId")),
        ("videoEntity.thumbId", entity.get("thumbId") if isinstance(entity, dict) else None),
    ])
    for source_key, value in thumb_id_candidates:
        thumb_id = _numeric_id(value)
        if thumb_id is not None:
            result.fields["thumb_id"] = thumb_id
            result.source_keys["thumb_id"] = source_key
            break

    selected_thumb_id = result.fields["thumb_id"] if result.fields["thumb_id"] is not None else existing_thumb_id
    entity_thumb = entity_thumbs.get(str(selected_thumb_id)) if selected_thumb_id is not None else None

    # thumbURL/imageURL semantics differ by page component. Prefer explicit fields
    # on a card proven to be the same canonical video; otherwise use videoModel
    # and videoEntity data belonging to the ID-verified primary page.
    thumbnail_candidates: list[tuple[str, Any]] = []
    if same_page_card is not None:
        thumbnail_candidates.append(("same_page_videoThumbProps.thumbURL", same_page_card.get("thumbURL")))
    thumbnail_candidates.extend([
        (f"videoEntity.thumbs[{selected_thumb_id}]", entity_thumb),
        ("videoModel.thumbURL", model.get("thumbURL")),
        ("videoModel.previewThumbURL", model.get("previewThumbURL")),
        ("videoEntity.thumbBig", entity.get("thumbBig") if isinstance(entity, dict) else None),
    ])
    _asset_candidate(
        result, "thumbnail_path", thumbnail_candidates, reject_high_res_for_thumbnail=True
    )

    image_candidates: list[tuple[str, Any]] = []
    if same_page_card is not None:
        image_candidates.append(("same_page_videoThumbProps.imageURL", same_page_card.get("imageURL")))
    image_candidates.extend([
        ("videoModel.imageURL", model.get("imageURL")),
        ("videoModel.thumbURL (high-resolution variant)", model.get("thumbURL") if is_high_resolution_image_url(model.get("thumbURL")) else None),
        ("videoEntity.thumbBig (high-resolution variant)", entity.get("thumbBig") if isinstance(entity, dict) and is_high_resolution_image_url(entity.get("thumbBig")) else None),
    ])
    _asset_candidate(result, "image_path", image_candidates)

    _asset_candidate(result, "preview_thumb_path", [
        ("videoModel.previewThumbURL", model.get("previewThumbURL")),
        ("same_page_videoThumbProps.previewThumbURL", same_page_card.get("previewThumbURL") if same_page_card else None),
    ])
    _asset_candidate(result, "sprite_path", [
        ("videoModel.spriteURL", model.get("spriteURL")),
        ("same_page_videoThumbProps.spriteURL", same_page_card.get("spriteURL") if same_page_card else None),
    ])
    _asset_candidate(result, "trailer_path", [
        ("videoModel.trailerURL", model.get("trailerURL")),
        ("same_page_videoThumbProps.trailerURL", same_page_card.get("trailerURL") if same_page_card else None),
    ])
    _asset_candidate(result, "trailer_fallback_path", [
        ("videoModel.trailerFallbackUrl", model.get("trailerFallbackUrl")),
        ("same_page_videoThumbProps.trailerFallbackUrl", same_page_card.get("trailerFallbackUrl") if same_page_card else None),
    ])

    result.fields["trailer_cdn_version"] = trailer_cdn_version(result.hosts.get("trailer_path"))
    if result.fields["trailer_cdn_version"] is not None:
        result.source_keys["trailer_cdn_version"] = "hostname of validated trailerURL"

    url_count = sum(result.fields.get(column) is not None for column in URL_COLUMNS)
    if url_count == 0:
        result.status = "no_media"
        result.reason_code = "no_media"
    elif url_count == len(URL_COLUMNS):
        result.status = "complete"
        result.reason_code = "complete"
    else:
        result.status = "partial"
        result.reason_code = "partial"
    if result.rejected_fields:
        result.error = "Rejected candidate fields: " + json.dumps(result.rejected_fields, ensure_ascii=True)[:700]
    return result


class HostPacer:
    """Space out HTTP request starts; throttle automatically on 429/503 responses."""
    def __init__(self, interval_seconds: float):
        self.interval = max(0.0, float(interval_seconds))
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self.lock:
            now = loop.time()
            delay = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if delay:
            await asyncio.sleep(delay)

    async def throttle(self) -> float:
        async with self.lock:
            self.interval = min(3.0, max(0.1, self.interval * 2 if self.interval else 0.1))
            return self.interval


class AdaptiveLimiter:
    """A concurrency semaphore whose maximum can be cut when the origin throttles."""
    def __init__(self, initial_limit: int):
        self.limit = max(1, int(initial_limit))
        self.active = 0
        self.condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self.condition:
            while self.active >= self.limit:
                await self.condition.wait()
            self.active += 1

    async def release(self) -> None:
        async with self.condition:
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    async def reduce(self) -> int:
        async with self.condition:
            self.limit = max(1, self.limit // 2)
            self.condition.notify_all()
            return self.limit


def _retry_after(response: aiohttp.ClientResponse, attempt: int) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    if raw.isdigit():
        return min(60.0, max(1.0, float(raw)))
    return min(30.0, (2 ** attempt) + random.uniform(0.25, 1.25))


async def _fetch_html(
    session: aiohttp.ClientSession,
    url: str,
    limiter: AdaptiveLimiter,
    pacer: HostPacer,
) -> tuple[str, str, int, str]:
    current = url
    for redirect_number in range(MAX_REDIRECTS + 1):
        if not _is_source_host(urlsplit(current).hostname or "") or urlsplit(current).scheme != "https":
            raise MediaExtractionError("redirect_rejected", "Source redirect escaped configured HTTPS host", terminal=True)
        await pacer.wait()
        async with session.get(current, allow_redirects=False) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location or redirect_number >= MAX_REDIRECTS:
                    raise MediaExtractionError("redirect_failure", "Source redirect limit was reached", http_status=response.status)
                next_url = urljoin(current, location)
                parsed = urlsplit(next_url)
                if (
                    parsed.scheme != "https"
                    or not _is_source_host(parsed.hostname or "")
                    or parsed.username
                    or parsed.password
                    or not parsed.path.startswith("/videos/")
                ):
                    raise MediaExtractionError(
                        "redirect_rejected", "Source redirected outside configured /videos/ pages",
                        terminal=True, http_status=response.status,
                    )
                current = next_url
                continue

            status = response.status
            content_type = response.headers.get("Content-Type", "")
            if status in {429, 503}:
                reduced = await limiter.reduce()
                interval = await pacer.throttle()
                retry_after = _retry_after(response, 0)
                raise MediaExtractionError(
                    "http_retryable",
                    f"source returned HTTP {status}; concurrency reduced to {reduced}, "
                    f"request interval raised to {interval:.2f}s, retry-after={retry_after:.1f}s",
                    http_status=status,
                )
            if status in {401, 403, 404, 410, 451}:
                code = "source_restricted" if status in {401, 403, 451} else "not_found"
                raise MediaExtractionError(
                    code,
                    f"source returned HTTP {status}; page will not be retried automatically",
                    terminal=True, http_status=status,
                )
            if status >= 500:
                raise MediaExtractionError("http_retryable", f"source returned HTTP {status}", http_status=status)
            if status < 200 or status >= 300:
                raise MediaExtractionError("http_failure", f"source returned HTTP {status}", http_status=status)
            if content_type and not any(kind in content_type.lower() for kind in ("text/html", "application/xhtml+xml", "text/plain")):
                raise MediaExtractionError(
                    "unexpected_content_type", f"expected HTML, received {content_type[:120]}",
                    terminal=True, http_status=status,
                )
            declared_size = response.headers.get("Content-Length", "")
            if declared_size.isdigit() and int(declared_size) > MAX_HTML_BYTES:
                raise MediaExtractionError("html_too_large", f"HTML exceeded {MAX_HTML_BYTES} byte limit", terminal=True, http_status=status)
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_HTML_BYTES:
                    response.close()
                    raise MediaExtractionError("html_too_large", f"HTML exceeded {MAX_HTML_BYTES} byte limit", terminal=True, http_status=status)
                chunks.append(chunk)
            charset = response.charset or "utf-8"
            body = b"".join(chunks).decode(charset, errors="replace")
            return body, str(response.url), status, content_type
    raise MediaExtractionError("redirect_failure", "Source redirect limit was reached", terminal=True)


def _failure_result(row: dict[str, Any], exc: MediaExtractionError, elapsed: float) -> MediaResult:
    status = "inaccessible" if exc.terminal else "retry"
    return MediaResult(
        video_id=int(row["id"]),
        page_url=str(row.get("page_path") or ""),
        status=status,
        reason_code=exc.code,
        error=str(exc)[:900],
        http_status=exc.http_status,
        elapsed_seconds=elapsed,
        fields={column: None for column in ALL_MEDIA_COLUMNS},
    )


async def extract_record(
    session: aiohttp.ClientSession,
    row: dict[str, Any],
    limiter: AdaptiveLimiter,
    pacer: HostPacer,
) -> MediaResult:
    started = time.perf_counter()
    video_id = int(row["id"])
    try:
        requested_url = _safe_video_url(row.get("page_path"))
    except MediaExtractionError as exc:
        return _failure_result(row, exc, time.perf_counter() - started)

    last_error: MediaExtractionError | None = None
    for attempt in range(HTTP_ATTEMPTS):
        try:
            await limiter.acquire()
            try:
                body, final_url, status_code, _content_type = await _fetch_html(session, requested_url, limiter, pacer)
            finally:
                await limiter.release()
            result = extract_media_from_html(
                body, video_id, final_url,
                existing_thumb_id=int(row["thumb_id"]) if row.get("thumb_id") is not None else None,
            )
            result.http_status = status_code
            result.elapsed_seconds = time.perf_counter() - started
            return result
        except MediaExtractionError as exc:
            last_error = exc
            if exc.terminal or attempt >= HTTP_ATTEMPTS - 1:
                break
            delay = min(60.0, (2 ** attempt) + random.uniform(0.2, 1.3))
            LOG.warning(
                "video_id=%s reason=%s attempt=%d/%d delay=%.1fs",
                video_id, exc.code, attempt + 1, HTTP_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = MediaExtractionError("network_failure", f"{type(exc).__name__}: {exc}")
            if attempt >= HTTP_ATTEMPTS - 1:
                break
            await asyncio.sleep(min(30.0, (2 ** attempt) + random.uniform(0.2, 1.0)))
        except SourceFormatError as exc:
            last_error = MediaExtractionError("parse_failure", str(exc))
            break
        except Exception as exc:
            last_error = MediaExtractionError("unexpected_error", f"{type(exc).__name__}: {exc}")
            break

    assert last_error is not None
    return _failure_result(row, last_error, time.perf_counter() - started)


def _is_empty(column: str, value: Any) -> bool:
    if value is None:
        return True
    return column in URL_COLUMNS and isinstance(value, str) and not value.strip()


def prepare_result_for_row(
    row: dict[str, Any],
    result: MediaResult,
    refresh_existing: bool,
) -> tuple[dict[str, Any], dict[str, str], list[str], str]:
    """Merge with old DB data without replacing any non-null value with null."""
    write_values: dict[str, Any] = {}
    host_values: dict[str, str] = {}
    effective: dict[str, Any] = {}

    for column in URL_COLUMNS:
        new_value = result.fields.get(column)
        old_value = row.get(column)
        can_write = (
            new_value is not None
            and (refresh_existing or _is_empty(column, old_value) or old_value == new_value)
        )
        if can_write:
            write_values[column] = new_value
            effective[column] = new_value
            if result.hosts.get(column):
                host_values[column] = result.hosts[column]
        else:
            effective[column] = old_value

    new_thumb_id = result.fields.get("thumb_id")
    old_thumb_id = row.get("thumb_id")
    if new_thumb_id is not None and (refresh_existing or old_thumb_id is None or old_thumb_id == new_thumb_id):
        write_values["thumb_id"] = new_thumb_id
        effective["thumb_id"] = new_thumb_id
    else:
        effective["thumb_id"] = old_thumb_id

    # This numeric shard belongs only to the trailer hostname, never the sprite host.
    new_version = result.fields.get("trailer_cdn_version")
    new_trailer = result.fields.get("trailer_path")
    effective_trailer = effective.get("trailer_path")
    trailer_is_aligned = new_trailer is not None and effective_trailer == new_trailer
    old_version = row.get("trailer_cdn_version")
    if trailer_is_aligned and new_version is not None and (
        refresh_existing or old_version is None or old_version == new_version
    ):
        write_values["trailer_cdn_version"] = new_version
        effective["trailer_cdn_version"] = new_version
    else:
        effective["trailer_cdn_version"] = old_version

    missing = [
        column for column in URL_COLUMNS if _is_empty(column, effective.get(column))
    ]
    if effective.get("thumb_id") is None:
        missing.append("thumb_id")
    if effective.get("trailer_path") and effective.get("trailer_cdn_version") is None:
        # The shard is optional on hosts with no thumb-vN marker. Report it as missing
        # but do not manufacture it or infer it from trailer_fallback_path / sprite_path.
        missing.append("trailer_cdn_version")

    present_urls = sum(not _is_empty(column, effective.get(column)) for column in URL_COLUMNS)
    if result.status in {"retry", "inaccessible"}:
        merged_status = result.status
    elif present_urls == 0:
        merged_status = "no_media"
    elif not missing:
        merged_status = "complete"
    else:
        merged_status = "partial"
    return write_values, host_values, missing, merged_status


def _selection_where(
    *,
    dry_run: bool,
    refresh_existing: bool,
    video_id: int | None,
) -> tuple[str, list[Any]]:
    if video_id is not None:
        return "v.id = %s", [video_id]
    if refresh_existing:
        missing = "TRUE"
    else:
        missing = "(" + " OR ".join(
            [f"NULLIF(v.{column}, '') IS NULL" for column in URL_COLUMNS]
            + [
                "v.thumb_id IS NULL",
                "(v.trailer_path IS NOT NULL AND v.trailer_cdn_version IS NULL)",
            ]
        ) + ")"
    where = [missing]
    params: list[Any] = []
    if not dry_run:
        where.append(
            "(e.video_id IS NULL OR "
            "(e.status = 'retry' AND e.attempts < %s "
            "AND (e.next_retry_at IS NULL OR e.next_retry_at <= now())))"
        )
        params.append(MAX_RECORD_ATTEMPTS)
    return " AND ".join(where), params


def count_eligible(
    conn,
    *,
    dry_run: bool,
    refresh_existing: bool,
    video_id: int | None,
) -> int:
    where, params = _selection_where(
        dry_run=dry_run, refresh_existing=refresh_existing, video_id=video_id
    )
    join = "" if dry_run else "LEFT JOIN public.video_media_enrichment e ON e.video_id = v.id"
    row = conn.execute(
        f"SELECT count(*) AS n FROM public.videos v {join} WHERE {where}", params
    ).fetchone()
    conn.commit()  # Do not hold a read transaction open while making HTTP requests.
    return int(row["n"])


def fetch_batch(
    conn,
    *,
    last_id: int,
    batch_size: int,
    dry_run: bool,
    refresh_existing: bool,
    video_id: int | None,
) -> list[dict[str, Any]]:
    where, params = _selection_where(
        dry_run=dry_run, refresh_existing=refresh_existing, video_id=video_id
    )
    join = "" if dry_run else "LEFT JOIN public.video_media_enrichment e ON e.video_id = v.id"
    tracker_attempts = "0::int AS attempts" if dry_run else "COALESCE(e.attempts, 0)::int AS attempts"
    cursor = "" if video_id is not None else " AND v.id > %s"
    if video_id is None:
        params.append(last_id)
    params.append(batch_size)
    columns = ", ".join(f"v.{column} AS {column}" for column in ALL_MEDIA_COLUMNS)
    rows = conn.execute(
        f"""
        SELECT v.id AS id, v.page_path AS page_path, v.title AS title,
               {columns}, {tracker_attempts}
        FROM public.videos v
        {join}
        WHERE {where}{cursor}
        ORDER BY v.id
        LIMIT %s
        """,
        params,
    ).fetchall()
    conn.commit()  # Each DB read is complete before page fetching starts.
    return [dict(row) for row in rows]


def _retry_delay_minutes(attempt: int) -> int:
    return min(720, 5 * (2 ** max(0, attempt - 1)))


def _tracker_row(
    row: dict[str, Any],
    result: MediaResult,
    merged_status: str,
    missing: list[str],
    host_values: dict[str, str],
) -> tuple[Any, ...]:
    prior_attempts = int(row.get("attempts") or 0)
    if result.status in {"retry", "inaccessible"}:
        attempts = min(prior_attempts + 1, 32767)
        if result.status == "inaccessible":
            status, retry_minutes = "inaccessible", None
        elif attempts >= MAX_RECORD_ATTEMPTS:
            status, retry_minutes = "exhausted", None
        else:
            status, retry_minutes = "retry", _retry_delay_minutes(attempts)
    else:
        status, attempts, retry_minutes = merged_status, 0, None

    last_error = result.error or result.reason_code or None
    return (
        int(row["id"]), status, attempts, retry_minutes, result.http_status,
        result.reason_code, str(last_error)[:900] if last_error else None,
        missing, json.dumps(host_values, ensure_ascii=True),
    )


def _bulk_update_videos(conn, rows: list[tuple[int, dict[str, Any]]], refresh_existing: bool) -> int:
    if not rows:
        return 0
    src_columns = ("id",) + ALL_MEDIA_COLUMNS
    values_sql: list[str] = []
    params: list[Any] = []
    for video_id, values in rows:
        values_sql.append("(" + ", ".join("%s::" + COLUMN_CASTS[column] for column in src_columns) + ")")
        params.extend([video_id] + [values.get(column) for column in ALL_MEDIA_COLUMNS])

    assignments = []
    for column in URL_COLUMNS:
        if refresh_existing:
            expression = f"COALESCE(src.{column}, NULLIF(v.{column}, ''))"
        else:
            expression = f"COALESCE(NULLIF(v.{column}, ''), src.{column})"
        assignments.append(f"{column} = {expression}")
    for column in ("thumb_id", "trailer_cdn_version"):
        expression = f"COALESCE(src.{column}, v.{column})" if refresh_existing else f"COALESCE(v.{column}, src.{column})"
        assignments.append(f"{column} = {expression}")

    sql = f"""
        UPDATE public.videos AS v
        SET {", ".join(assignments)}
        FROM (VALUES {", ".join(values_sql)}) AS src({", ".join(src_columns)})
        WHERE v.id = src.id
    """
    return conn.execute(sql, params).rowcount


def _bulk_write_tracker(conn, tracker_rows: list[tuple[Any, ...]]) -> None:
    if not tracker_rows:
        return
    row_sql = "(" + ", ".join(
        ["%s::BIGINT", "%s::TEXT", "%s::SMALLINT", "%s::INT", "%s::INT",
         "%s::TEXT", "%s::TEXT", "%s::TEXT[]", "%s::JSONB"]
    ) + ")"
    values_sql = ", ".join(row_sql for _ in tracker_rows)
    params: list[Any] = []
    for row in tracker_rows:
        params.extend(row)
    conn.execute(
        f"""
        INSERT INTO public.video_media_enrichment
            (video_id, status, attempts, retry_minutes, last_http_status,
             reason_code, last_error, missing_fields, media_hosts)
        SELECT src.video_id, src.status, src.attempts,
               CASE WHEN src.retry_minutes IS NULL THEN NULL
                    ELSE now() + (src.retry_minutes * interval '1 minute') END,
               src.last_http_status, src.reason_code, src.last_error,
               src.missing_fields, src.media_hosts
        FROM (VALUES {values_sql}) AS src(
            video_id, status, attempts, retry_minutes, last_http_status,
            reason_code, last_error, missing_fields, media_hosts
        )
        ON CONFLICT (video_id) DO UPDATE SET
            status = EXCLUDED.status,
            attempts = EXCLUDED.attempts,
            next_retry_at = EXCLUDED.next_retry_at,
            last_attempt_at = now(),
            last_http_status = EXCLUDED.last_http_status,
            reason_code = EXCLUDED.reason_code,
            last_error = EXCLUDED.last_error,
            missing_fields = EXCLUDED.missing_fields,
            media_hosts = COALESCE(public.video_media_enrichment.media_hosts, '{}'::jsonb) || EXCLUDED.media_hosts,
            updated_at = now()
        """,
        params,
    )


def _diagnostic_payload(
    row: dict[str, Any],
    result: MediaResult,
    refresh_existing: bool,
) -> dict[str, Any]:
    writes, hosts, missing, merged_status = prepare_result_for_row(row, result, refresh_existing)
    return {
        "video_id": result.video_id,
        "title": row.get("title"),
        "requested_page_path": row.get("page_path"),
        "primary_model_id": result.primary_model_id,
        "canonical_primary_page": result.page_url,
        "status": merged_status,
        "reason_code": result.reason_code,
        "http_status": result.http_status,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "extracted_fields": result.fields,
        "normalized_paths_to_write": writes,
        "source_json_keys": result.source_keys,
        "cdn_hosts_for_written_paths": hosts,
        "rejected_candidates": result.rejected_fields,
        "missing_after_merge": missing,
        "error": result.error,
    }


async def _process_batch(
    rows: list[dict[str, Any]],
    session: aiohttp.ClientSession,
    limiter: AdaptiveLimiter,
    pacer: HostPacer,
) -> list[MediaResult]:
    # The queue stays bounded to this database batch and only a fixed number of workers.
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=max(1, len(rows)))
    results: dict[int, MediaResult] = {}

    async def worker() -> None:
        while True:
            row = await queue.get()
            try:
                if row is None:
                    return
                results[int(row["id"])] = await extract_record(session, row, limiter, pacer)
            finally:
                queue.task_done()

    count = min(len(rows), max(1, limiter.limit))
    workers = [asyncio.create_task(worker()) for _ in range(count)]
    for row in rows:
        await queue.put(row)
    for _ in workers:
        await queue.put(None)
    await queue.join()
    await asyncio.gather(*workers)
    return [results[int(row["id"])] for row in rows]


async def _main_async(conn, args, rows_total: int, stats: dict[str, int], field_counts: dict[str, int]) -> None:
    seed = http_session()
    cookie_values = seed.cookies.get_dict()
    headers = {
        "User-Agent": seed.headers.get("User-Agent", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": seed.headers.get("Accept-Language", "en-US,en;q=0.8"),
    }
    timeout = aiohttp.ClientTimeout(
        total=max(10, TIMEOUT),
        connect=min(15, max(5, TIMEOUT)),
        sock_read=max(10, TIMEOUT),
    )
    connector = aiohttp.TCPConnector(
        limit=args.concurrency,
        limit_per_host=args.concurrency,
        enable_cleanup_closed=True,
    )
    limiter = AdaptiveLimiter(args.concurrency)
    pacer = HostPacer(args.min_request_interval)
    start = time.perf_counter()
    last_id = -1

    async with aiohttp.ClientSession(
        cookies=cookie_values,
        headers=headers,
        timeout=timeout,
        connector=connector,
        raise_for_status=False,
    ) as session:
        while stats["processed"] < rows_total:
            remaining = rows_total - stats["processed"]
            rows = fetch_batch(
                conn,
                last_id=last_id,
                batch_size=min(args.batch_size, remaining),
                dry_run=args.dry_run,
                refresh_existing=args.refresh_existing,
                video_id=args.video_id,
            )
            if not rows:
                break
            if args.video_id is None:
                last_id = int(rows[-1]["id"])
            results = await _process_batch(rows, session, limiter, pacer)

            pending_updates: list[tuple[int, dict[str, Any]]] = []
            pending_tracker: list[tuple[Any, ...]] = []
            for row, result in zip(rows, results):
                result.attempts_before = int(row.get("attempts") or 0)
                stats["processed"] += 1
                writes, hosts, missing, merged_status = prepare_result_for_row(
                    row, result, args.refresh_existing
                )

                if result.status == "inaccessible":
                    stats["inaccessible"] += 1
                elif result.status == "retry":
                    stats["retry"] += 1
                elif merged_status == "no_media":
                    stats["no_media"] += 1
                elif merged_status == "complete":
                    stats["complete"] += 1
                else:
                    stats["partial"] += 1

                for column in ALL_MEDIA_COLUMNS:
                    if result.fields.get(column) is not None:
                        field_counts[column] += 1

                if args.dry_run:
                    if args.video_id is not None:
                        print(json.dumps(_diagnostic_payload(row, result, args.refresh_existing), ensure_ascii=False, indent=2))
                    else:
                        LOG.info(
                            "DRY RUN video_id=%s status=%s extracted_urls=%d reason=%s",
                            result.video_id, merged_status,
                            sum(result.fields.get(column) is not None for column in URL_COLUMNS),
                            result.reason_code,
                        )
                else:
                    if writes:
                        pending_updates.append((int(row["id"]), writes))
                    pending_tracker.append(
                        _tracker_row(row, result, merged_status, missing, hosts)
                    )

            if not args.dry_run and (pending_updates or pending_tracker):
                with conn.transaction():
                    stats["database_rows_updated"] += _bulk_update_videos(
                        conn, pending_updates, args.refresh_existing
                    )
                    _bulk_write_tracker(conn, pending_tracker)

            elapsed = max(0.001, time.perf_counter() - start)
            LOG.info(
                "Processed %d/%d | complete=%d partial=%d no_media=%d inaccessible=%d retry=%d "
                "updated_rows=%d throughput=%.2f videos/s",
                stats["processed"], rows_total, stats["complete"], stats["partial"],
                stats["no_media"], stats["inaccessible"], stats["retry"],
                stats["database_rows_updated"], stats["processed"] / elapsed,
            )
            if args.video_id is not None:
                break


def run(args: argparse.Namespace) -> int:
    if not DATABASE_URL:
        LOG.error("Set DATABASE_URL in the repository Actions secrets.")
        return 2
    if not SITE_BASE_URL or urlsplit(SITE_BASE_URL).scheme != "https":
        LOG.error("Set SITE_BASE_URL to a valid HTTPS source origin.")
        return 2
    if args.batch_size < 1 or args.batch_size > 5000:
        LOG.error("--batch-size must be between 1 and 5000.")
        return 2
    if args.concurrency < 1 or args.concurrency > 100:
        LOG.error("--concurrency must be between 1 and 100.")
        return 2
    if args.max_videos < 0:
        LOG.error("--max-videos must be >= 0.")
        return 2
    if args.min_request_interval < 0 or args.min_request_interval > 10:
        LOG.error("--min-request-interval must be between 0 and 10 seconds.")
        return 2
    if args.video_id is not None and args.video_id < 0:
        LOG.error("--video-id must be a non-negative integer.")
        return 2

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.info(
        "Media Enrichment Started | batch=%d concurrency=%d max_videos=%s dry_run=%s refresh_existing=%s",
        args.batch_size, args.concurrency,
        "unlimited" if args.max_videos == 0 else args.max_videos,
        args.dry_run, args.refresh_existing,
    )

    stats = {
        "processed": 0, "complete": 0, "partial": 0, "no_media": 0,
        "inaccessible": 0, "retry": 0, "database_rows_updated": 0,
    }
    field_counts = {column: 0 for column in ALL_MEDIA_COLUMNS}
    started = time.perf_counter()
    try:
        with db() as conn:
            if not args.dry_run:
                with conn.transaction():
                    conn.execute(TRACKER_DDL, prepare=False)
            total = count_eligible(
                conn, dry_run=args.dry_run,
                refresh_existing=args.refresh_existing, video_id=args.video_id,
            )
            if args.max_videos:
                total = min(total, args.max_videos)
            LOG.info("Total eligible videos: %d", total)
            if total == 0:
                if args.video_id is not None:
                    LOG.error("Video id %s was not found or is not eligible.", args.video_id)
                    return 2
                LOG.info("No eligible videos.")
                return 0
            asyncio.run(_main_async(conn, args, total, stats, field_counts))
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Completed database batches remain committed; rerun to resume.")
        return 130
    except Exception:
        LOG.exception("Media enrichment failed.")
        return 1

    elapsed = max(0.001, time.perf_counter() - started)
    LOG.info("Media Enrichment Finished")
    LOG.info(
        "Processed=%d complete=%d partial=%d no_media=%d inaccessible=%d retry=%d",
        stats["processed"], stats["complete"], stats["partial"], stats["no_media"],
        stats["inaccessible"], stats["retry"],
    )
    for column, count in field_counts.items():
        LOG.info("%s extracted: %d", column, count)
    LOG.info(
        "Database rows updated=%d; average=%.3fs/video; throughput=%.2f videos/s",
        stats["database_rows_updated"], elapsed / max(1, stats["processed"]),
        stats["processed"] / elapsed,
    )
    # Expected missing optional media and access-restricted pages are reported but
    # do not fail an otherwise successful batch. Retryable worker errors do.
    return 1 if stats["retry"] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("MEDIA_BATCH_SIZE", "500")))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("MEDIA_CONCURRENCY", "20")))
    parser.add_argument("--max-videos", type=int, default=int(os.getenv("MEDIA_MAX_VIDEOS", "0")),
                        help="0 means no application-level limit")
    parser.add_argument("--video-id", type=int, default=None,
                        help="diagnose one existing public.videos.id without requiring it to be missing")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="extract and report without database writes (default)")
    parser.add_argument("--apply", action="store_true",
                        help="persist validated paths and update the retry checkpoint table")
    parser.add_argument("--refresh-existing", action="store_true", default=False,
                        help="allow validated media paths to replace populated paths; null never overwrites")
    parser.add_argument("--min-request-interval", type=float,
                        default=float(os.getenv("MEDIA_MIN_REQUEST_INTERVAL", "0.05")),
                        help="minimum seconds between request starts to the source host")
    args = parser.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
