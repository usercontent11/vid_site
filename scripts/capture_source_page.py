#!/usr/bin/env python3
"""Capture a sanitized source-page HTML snapshot and a structure-only report."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolver import _read_source_page, inspect_page_structure, source_page_url

OUTPUT_DIR = ROOT / "diagnostics"
MAX_SNAPSHOT_CHARS = 4 * 1024 * 1024

SENSITIVE_KEY_PATTERN = (
    r"(?:access[_-]?token|refresh[_-]?token|token|csrf(?:[_-]?token)?|"
    r"authorization|cookie|set-cookie|session(?:[_-]?id)?|signature|sig|"
    r"secret|password|api[_-]?key|stats[_-]?tkn|g_state)"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""(?i)(["']?""" + SENSITIVE_KEY_PATTERN + r"""["']?\s*[:=]\s*)"""
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
)
QUERY_VALUE_RE = re.compile(
    r"""((?:[?&]|\\u003[fF]|\\u0026)[A-Za-z0-9_.~%+-]+(?:=|\\u003[dD]))"""
    r"""[^&#\s"'<>\\]*"""
)


def redact_html(page_html: str) -> str:
    """Preserve markup and keys while removing query and credential values."""
    safe = page_html[:MAX_SNAPSHOT_CHARS]
    safe = SENSITIVE_ASSIGNMENT_RE.sub(r'\1"[REDACTED]"', safe)
    safe = QUERY_VALUE_RE.sub(r"\1[REDACTED]", safe)
    safe = re.sub(
        r"""(?is)(<meta\b[^>]*(?:name|property)\s*=\s*["'][^"']*"""
        r"""(?:csrf|token|auth|secret|session)[^"']*["'][^>]*content\s*=\s*)"""
        r"""(["'])[^"']*\2""",
        r'\1"[REDACTED]"',
        safe,
    )
    return safe


def main() -> int:
    page_path = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("PAGE_PATH", "")).strip()
    base_url = os.getenv("SITE_BASE_URL", "https://mobilexh.live").strip().rstrip("/")
    if not page_path.startswith("/videos/"):
        print("PAGE_PATH must be a path beginning with /videos/; no request was made.")
        return 2

    try:
        page_url = source_page_url(page_path, base_url)
        source_host = (urlsplit(base_url).hostname or "").lower()
        final_url, body = _read_source_page(page_url, source_host)
    except Exception as exc:
        print(f"Source page capture failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 1

    report = inspect_page_structure(body, final_url)
    report["capture"] = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_path": page_path,
        "response_characters": len(body),
        "snapshot_redacted": True,
        "redactions": [
            "all URL query parameter values",
            "credential/session-like assignment values",
            "matching secret-like HTML meta content values",
        ],
        "raw_response_committed": False,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "source-page-response.html").write_text(
        redact_html(body), encoding="utf-8"
    )
    (OUTPUT_DIR / "source-page-structure.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Captured source page: "
        f"title={report['page']['title']!r}, "
        f"html_characters={len(body)}, "
        f"scripts={report['script_count']}, "
        f"links={report['link_count']}, "
        f"media_elements={report['media_element_count']}. "
        "Saved sanitized HTML and structure JSON."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
