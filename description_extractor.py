#!/usr/bin/env python3
"""Small, bounded async test of videoModel.description extraction into PostgreSQL."""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

from extractor import (
    DATABASE_URL,
    SITE_BASE_URL,
    TIMEOUT,
    SourceFormatError,
    db,
    ensure_schema,
    http_session,
    parse_initials,
)

LOG = logging.getLogger("description-extractor")
MAX_TEST_LIMIT = 50
MAX_WORKERS = 8
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
MAX_HTTP_ATTEMPTS = 4
DIAGNOSTIC_DIR = Path(os.getenv("DESCRIPTION_DIAGNOSTIC_DIR", "diagnostic"))
INITIALS_SCRIPT_PATTERN = re.compile(
    r'<script\b[^>]*\bid=["\']initials-script["\'][^>]*>(.*?)</script\s*>',
    re.IGNORECASE | re.DOTALL,
)


def extract_initials_script_source(html):
    """Return the exact inner contents of the source's initials-script element."""
    if not isinstance(html, str):
        return None
    match = INITIALS_SCRIPT_PATTERN.search(html)
    return match.group(1) if match else None


def save_initials_script_diagnostic(video, script_source, error, diagnostic_dir=None):
    """Write the raw initials-script and a small JSON sidecar for debugging only."""
    if script_source is None:
        return None
    target_dir = Path(diagnostic_dir) if diagnostic_dir is not None else DIAGNOSTIC_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    video_id = int(video["id"])
    script_name = f"video-{video_id}-initials-script.js"
    script_path = target_dir / script_name
    script_path.write_text(script_source, encoding="utf-8")
    metadata = {
        "video_id": video_id,
        "title": video.get("title"),
        "page_path": video.get("page_path"),
        "error": str(error),
        "script_file": script_name,
        "script_characters": len(script_source),
    }
    (target_dir / f"video-{video_id}-diagnostic.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return script_path


def extract_video_description(html):
    """Read only window.initials.videoModel.description; never use videoDescription."""
    initials = parse_initials(html)
    model = initials.get("videoModel")
    if not isinstance(model, dict):
        raise SourceFormatError("window.initials.videoModel is missing or is not an object.")
    if "description" not in model:
        raise SourceFormatError("videoModel.description is missing; videoDescription is not a fallback.")
    value = model["description"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceFormatError(f"videoModel.description must be a string or null, got {type(value).__name__}.")
    return value.strip()


def make_video_url(path):
    """Only allow stored same-site /videos/... paths; never request an arbitrary database URL."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("page_path is empty.")
    parsed = urlsplit(path.strip())
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("page_path must be a path without domain, query, or fragment.")
    if not parsed.path.startswith("/videos/") or parsed.path.startswith("//"):
        raise ValueError(f"Unexpected video path {path!r}; expected a /videos/... path.")
    return urljoin(SITE_BASE_URL.rstrip("/") + "/", quote(parsed.path, safe="/%:@"))


async def fetch_one(session, video, semaphore, timeout, base_host):
    html = None
    result = {
        "id": int(video["id"]),
        "title": video["title"],
        "page_path": video["page_path"],
        "status": "error",
        "description": None,
        "error": None,
    }
    try:
        url = make_video_url(video["page_path"])
    except ValueError as exc:
        result["error"] = str(exc)
        result["status"] = "invalid_path"
        return result

    async with semaphore:
        for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
            try:
                async with session.get(url, allow_redirects=True, timeout=timeout) as response:
                    if response.status in TRANSIENT_STATUSES:
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            delay = min(15.0, max(0.5, float(retry_after)))
                        except ValueError:
                            delay = min(8.0, 0.75 * (2 ** (attempt - 1)))
                        await response.read()
                        if attempt < MAX_HTTP_ATTEMPTS:
                            LOG.warning("HTTP %s for video %s; retry %s/%s after %.1fs.",
                                        response.status, result["id"], attempt + 1, MAX_HTTP_ATTEMPTS, delay)
                            await asyncio.sleep(delay)
                            continue
                        result["error"] = f"HTTP {response.status} after {attempt} attempts"
                        result["status"] = "http_error"
                        return result
                    if response.status >= 400:
                        result["error"] = f"HTTP {response.status}"
                        result["status"] = "http_error"
                        return result
                    if (response.url.host or "").lower() != base_host:
                        result["error"] = f"Redirected to unexpected host {(response.url.host or '')!r}"
                        result["status"] = "redirect_rejected"
                        return result
                    html = await response.text(errors="replace")

                description = extract_video_description(html)
                if description is None:
                    result["status"] = "null_description"
                else:
                    result["description"] = description
                    result["status"] = "ok"
                return result
            except SourceFormatError as exc:
                result["error"] = str(exc)
                result["status"] = "parse_error"
                script_source = extract_initials_script_source(html) if html is not None else None
                if script_source is not None:
                    try:
                        diagnostic_path = save_initials_script_diagnostic(video, script_source, exc)
                        LOG.warning(
                            "Saved extracted initials-script for video %s to %s",
                            result["id"], diagnostic_path,
                        )
                    except OSError:
                        LOG.exception("Could not write initials-script diagnostic for video %s.", result["id"])
                else:
                    LOG.warning("No initials-script element was available to save for video %s.", result["id"])
                return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= MAX_HTTP_ATTEMPTS:
                    result["error"] = f"{type(exc).__name__}: {exc}"[:500]
                    result["status"] = "request_error"
                    return result
                delay = min(8.0, 0.75 * (2 ** (attempt - 1)))
                LOG.warning("Request error for video %s (%s); retrying in %.1fs.",
                            result["id"], type(exc).__name__, delay)
                await asyncio.sleep(delay)
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"[:500]
                result["status"] = "error"
                return result

    return result


def load_sample(limit):
    with db() as conn:
        return conn.execute(
            """
            SELECT id, title, page_path
            FROM videos
            WHERE description IS NULL
              AND page_path LIKE '/videos/%%'
            ORDER BY views DESC NULLS LAST, id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


def save_descriptions(results):
    rows = [
        {
            "id": item["id"],
            "title": item["title"],
            "page_path": item["page_path"],
            "description": item["description"],
        }
        for item in results
        if item["status"] == "ok" and item["description"] is not None
    ]
    if not rows:
        return 0
    sql = """
    INSERT INTO videos(id,title,page_path,description)
    VALUES (%(id)s,%(title)s,%(page_path)s,%(description)s)
    ON CONFLICT(id) DO UPDATE SET description=EXCLUDED.description
    WHERE EXCLUDED.description IS NOT NULL
    """
    with db() as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


async def run_test(videos, workers):
    seed_session = http_session()
    cookies = seed_session.cookies.get_dict()
    headers = {
        "User-Agent": seed_session.headers.get("User-Agent", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": seed_session.headers.get("Accept-Language", "en-US,en;q=0.8"),
    }
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    connector = aiohttp.TCPConnector(limit=workers, limit_per_host=workers)
    semaphore = asyncio.Semaphore(workers)
    base_host = (urlsplit(SITE_BASE_URL).hostname or "").lower()
    async with aiohttp.ClientSession(
        cookies=cookies,
        headers=headers,
        timeout=timeout,
        connector=connector,
    ) as session:
        return await asyncio.gather(*[
            fetch_one(session, video, semaphore, timeout, base_host)
            for video in videos
        ])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Test video description extraction on a small sample and upsert successful results."
    )
    parser.add_argument(
        "--limit", type=int, default=int(os.getenv("DESCRIPTION_LIMIT", "5")),
        help="sample size, 1-50 (default: 5)",
    )
    parser.add_argument(
        "--workers", type=int, default=int(os.getenv("DESCRIPTION_WORKERS", "3")),
        help="concurrent HTTP requests, 1-8 (default: 3)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not DATABASE_URL:
        LOG.error("Set DATABASE_URL in GitHub Actions secrets.")
        return 2
    if not SITE_BASE_URL:
        LOG.error("Set SITE_BASE_URL in GitHub Actions variables or secrets.")
        return 2
    if not 1 <= args.limit <= MAX_TEST_LIMIT:
        LOG.error("--limit must be between 1 and %d; this test workflow is intentionally bounded.", MAX_TEST_LIMIT)
        return 2
    if not 1 <= args.workers <= MAX_WORKERS:
        LOG.error("--workers must be between 1 and %d.", MAX_WORKERS)
        return 2

    try:
        ensure_schema()
        videos = load_sample(args.limit)
        if not videos:
            LOG.info("No videos with a NULL description were found in the requested sample.")
            return 0
        LOG.info("Testing %d videos with %d async HTTP workers (sample limit=%d).",
                 len(videos), args.workers, args.limit)
        results = asyncio.run(run_test(videos, args.workers))
        saved = save_descriptions(results)
    except Exception:
        LOG.exception("Description test failed.")
        return 1

    counts = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        if item["status"] == "ok":
            # Show only a short preview for debugging; the complete string is stored in PostgreSQL.
            preview = " ".join(item["description"].split())[:140]
            LOG.info("Video %s: saved description (%d chars): %s%s",
                     item["id"], len(item["description"]), preview,
                     "…" if len(item["description"]) > 140 else "")
        else:
            LOG.warning("Video %s: %s%s", item["id"], item["status"],
                        f" — {item['error']}" if item["error"] else "")
    LOG.info("Description test complete: sampled=%d, saved=%d, outcomes=%s",
             len(results), saved, json.dumps(counts, sort_keys=True))
    return 0 if all(item["status"] in {"ok", "null_description"} for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
