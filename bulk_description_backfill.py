#!/usr/bin/env python3
"""Resumable full-scale backfill for videos with missing descriptions."""
import argparse
import asyncio
import json
import logging
import os
import sys

from description_extractor import (
    DATABASE_URL,
    SITE_BASE_URL,
    MAX_WORKERS,
    db,
    ensure_schema,
    run_test,
    save_descriptions,
)

LOG = logging.getLogger("description-backfill")
MAX_BATCH_SIZE = 500
MAX_TOTAL_VIDEOS = 2_000_000


def count_missing():
    with db() as conn:
        row = conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE description IS NULL) AS all_missing,
              count(*) FILTER (
                WHERE description IS NULL AND page_path LIKE '/videos/%'
              ) AS eligible_missing
            FROM videos
            """
        ).fetchone()
    return int(row["all_missing"]), int(row["eligible_missing"])


def load_batch(after_id, batch_size):
    """Get a stable ID-ordered batch; failed IDs are not repeated within this run."""
    with db() as conn:
        return conn.execute(
            """
            SELECT id, title, page_path
            FROM videos
            WHERE description IS NULL
              AND page_path LIKE '/videos/%%'
              AND (%s::bigint IS NULL OR id > %s)
            ORDER BY id ASC
            LIMIT %s
            """,
            (after_id, after_id, batch_size),
        ).fetchall()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill descriptions for every video that still has a NULL description."
    )
    parser.add_argument(
        "--batch-size", type=int,
        default=int(os.getenv("DESCRIPTION_BATCH_SIZE", "100")),
        help="database batch size, 1-500 (default: 100)",
    )
    parser.add_argument(
        "--workers", type=int,
        default=int(os.getenv("DESCRIPTION_WORKERS", "5")),
        help="concurrent HTTP requests, 1-8 (default: 5)",
    )
    parser.add_argument(
        "--max-videos", type=int,
        default=int(os.getenv("DESCRIPTION_MAX_VIDEOS", "0")),
        help="optional cap for this run; 0 means process all remaining videos",
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
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        LOG.error("--batch-size must be between 1 and %d.", MAX_BATCH_SIZE)
        return 2
    if not 1 <= args.workers <= MAX_WORKERS:
        LOG.error("--workers must be between 1 and %d.", MAX_WORKERS)
        return 2
    if not 0 <= args.max_videos <= MAX_TOTAL_VIDEOS:
        LOG.error("--max-videos must be 0 (all) or between 1 and %d.", MAX_TOTAL_VIDEOS)
        return 2

    try:
        ensure_schema()
        initial_all_missing, initial_eligible = count_missing()
    except Exception:
        LOG.exception("Could not initialize the database or count missing descriptions.")
        return 1

    if initial_eligible == 0:
        LOG.info(
            "No eligible videos with NULL descriptions. Missing descriptions total=%d "
            "(%d may have page paths outside /videos/).",
            initial_all_missing, initial_all_missing,
        )
        return 0

    target = min(initial_eligible, args.max_videos) if args.max_videos else initial_eligible
    LOG.info(
        "Backfill started: eligible_missing=%d, all_missing=%d, target_this_run=%d, "
        "batch_size=%d, workers=%d. max-videos=0 means all eligible rows.",
        initial_eligible, initial_all_missing, target, args.batch_size, args.workers,
    )

    after_id = None
    processed = 0
    saved_total = 0
    batch_number = 0
    outcomes = {}

    while processed < target:
        request_size = min(args.batch_size, target - processed)
        try:
            videos = load_batch(after_id, request_size)
            if not videos:
                break
            after_id = int(videos[-1]["id"])
            batch_number += 1
            results = asyncio.run(run_test(videos, args.workers))
            saved = save_descriptions(results)
        except Exception:
            LOG.exception(
                "Backfill stopped after %d videos were examined (last_id=%s). "
                "Rerun this workflow to resume; completed descriptions are skipped.",
                processed, after_id,
            )
            return 1

        processed += len(videos)
        saved_total += saved
        batch_outcomes = {}
        for item in results:
            status = item["status"]
            outcomes[status] = outcomes.get(status, 0) + 1
            batch_outcomes[status] = batch_outcomes.get(status, 0) + 1

        LOG.info(
            "Batch %d complete: examined=%d/%d, last_id=%s, saved_this_batch=%d, "
            "outcomes=%s",
            batch_number, processed, target, after_id, saved,
            json.dumps(batch_outcomes, sort_keys=True),
        )

    try:
        remaining_all, remaining_eligible = count_missing()
    except Exception:
        LOG.exception("Backfill completed, but final missing-description counts failed.")
        return 1

    unresolved = sum(count for status, count in outcomes.items() if status != "ok")
    LOG.info(
        "Description backfill summary: initial_eligible=%d, target=%d, examined=%d, "
        "saved=%d, unresolved_in_this_run=%d, outcomes=%s, remaining_eligible=%d, "
        "remaining_all_missing=%d",
        initial_eligible, target, processed, saved_total, unresolved,
        json.dumps(outcomes, sort_keys=True), remaining_eligible, remaining_all,
    )
    if args.max_videos and processed >= args.max_videos and remaining_eligible:
        LOG.info(
            "This run reached --max-videos=%d; run again to continue. Successful rows "
            "are skipped automatically.",
            args.max_videos,
        )
    elif remaining_eligible:
        LOG.warning(
            "%d eligible rows still have NULL descriptions. They were not overwritten with "
            "placeholders; rerun to retry failures or inspect diagnostic artifacts.",
            remaining_eligible,
        )
    if remaining_all > remaining_eligible:
        LOG.warning(
            "%d rows have NULL descriptions but a page_path outside /videos/ and were "
            "not requested.",
            remaining_all - remaining_eligible,
        )

    hard_failures = sum(
        count for status, count in outcomes.items()
        if status not in {"ok", "null_description"}
    )
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
