#!/usr/bin/env python3
"""Extract image-like assets from a source video page and generate a browsable HTML gallery."""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "diagnostics" / "image-extraction-test.html"
DEFAULT_PATH = "/videos/1193895"
CDN_HOST = "ic-vt-nss.cdnsolutions.media"
IMAGE_EXT = r"(?:avif|bmp|gif|ico|jpe?g|png|svg|webp|apng|tiff?)"
VIDEO_EXT = r"(?:mp4|m4v|mov|webm|mpd|m3u8|ts|m4s)"
URL_RE = re.compile(
    r'''(?i)(?:https?:)?//[^\s"'<>\\]+|/(?!/)[^\s"'<>\\]{1,600}?\.(?:'''
    + IMAGE_EXT
    + r''')(?:[?#][^\s"'<>\\]*)?'''
)
EXT_RE = re.compile(r"\." + IMAGE_EXT + r"(?:$|[?#])", re.I)
VIDEO_RE = re.compile(r"\." + VIDEO_EXT + r"(?:$|[?#])", re.I)
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.I | re.S)
SRCSET_SPLIT_RE = re.compile(r"""\s*,\s*(?=(?:https?:)?//|/)""")


def normalize_url(value: str, page_url: str) -> str | None:
    value = html.unescape(str(value or "")).strip().strip("\"' ")
    value = value.removesuffix(")").removesuffix(",").removesuffix(";")
    value = value.replace(r"\/", "/")
    value = re.sub(r"\\u002f", "/", value, flags=re.I)
    value = re.sub(r"\\u0026", "&", value, flags=re.I)
    value = re.sub(r"\\u003d", "=", value, flags=re.I)
    value = re.sub(r"\\u003f", "?", value, flags=re.I)
    if not value or value.lower().startswith(("data:", "blob:", "javascript:", "mailto:")):
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(urljoin(page_url, value))
        if parsed.scheme == "http" and (parsed.hostname or "").lower().endswith(".xhcdn.com"):
            parsed = parsed._replace(scheme="https")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return parsed.geturl()
    except (ValueError, TypeError):
        return None


def rewrite_cdn(value: str) -> str:
    """Rewrite XH CDN image hostnames to the requested image CDN, keeping path/query."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    is_xh_image_host = host.endswith(".xhcdn.com") and (
        host.startswith("ic-") or host.startswith("thumb-")
    )
    is_cdn_image_host = host.endswith(".cdnsolutions.media") and (
        host.startswith("ic-") or host.startswith("thumb-")
    )
    if not (is_xh_image_host or is_cdn_image_host):
        return value
    return urlunsplit(("https", CDN_HOST, parsed.path, parsed.query, parsed.fragment))


def image_like(value: str, context: str = "", allow_path_without_extension: bool = False) -> bool:
    parsed = urlsplit(value)
    path = parsed.path.lower()
    if VIDEO_RE.search(path):
        return False
    if EXT_RE.search(path):
        return True
    hint = context.lower()
    return allow_path_without_extension and any(
        word in hint for word in ("sprite", "poster", "og:image", "twitter:image", "thumbnail", "thumburl", "imageurl")
    )


def classify(context: str, value: str) -> str:
    text = (context + " " + urlsplit(value).path).lower()
    if "sprite" in text:
        return "Sprite sheet"
    if "poster" in text:
        return "Video poster"
    if "thumbnail" in text or re.search(r"\bthumb\b|thumb[-_]?url", text):
        return "Thumbnail / preview"
    if "og:image" in text or "twitter:image" in text or "social image" in text:
        return "Social / metadata image"
    if "favicon" in text or "icon" in text or "apple-touch" in text:
        return "Icon"
    if "srcset" in text or "imagesrcset" in text:
        return "Responsive image"
    if "preload" in text:
        return "Preloaded image"
    if "css" in text or "background" in text or "style" in text:
        return "CSS / background image"
    return "Image"


class AssetExtractor(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.section = "document"
        self.stylesheets: list[str] = []
        self.external_css_count = 0
        self.active_text_tag: str | None = None
        self.active_text: list[str] = []
        self.active_text_attrs: dict[str, str] = {}
        self.picture_depth = 0
        self.found: dict[str, dict] = {}
        self.script_counter = 0

    def _add(self, raw_value: str, source: str, context: str, *, allow_no_ext: bool = False, base_url: str | None = None):
        value = normalize_url(raw_value, base_url or self.page_url)
        if not value or not image_like(value, context, allow_no_ext):
            return
        rewritten = rewrite_cdn(value)
        kind = classify(context + " " + source, value)
        record = self.found.get(rewritten)
        if record is None:
            record = {"url": rewritten, "kind": kind, "original_urls": [],
                      "locations": [], "source_host": (urlsplit(value).hostname or "")}
            self.found[rewritten] = record
        if value not in record["original_urls"] and len(record["original_urls"]) < 8:
            record["original_urls"].append(value)
        location = {"section": self.section, "source": source, "kind": kind, "context": context[:180]}
        if location not in record["locations"] and len(record["locations"]) < 16:
            record["locations"].append(location)
        priority = {"Sprite sheet": 0, "Video poster": 1, "Thumbnail / preview": 2,
                    "Social / metadata image": 3, "Preloaded image": 4, "Responsive image": 5,
                    "CSS / background image": 6, "Icon": 7, "Image": 8}
        if priority.get(kind, 99) < priority.get(record["kind"], 99):
            record["kind"] = kind

    def _srcset(self, value: str, source: str, context: str):
        # Split only at commas followed by another URL start, not CDN transform commas.
        for part in SRCSET_SPLIT_RE.split(value):
            candidate = part.strip().split()[0] if part.strip() else ""
            if candidate:
                self._add(candidate, source, context, allow_no_ext=True)

    def _css(self, value: str, source: str, context: str, base_url: str | None = None):
        for match in CSS_URL_RE.finditer(value or ""):
            candidate = match.group(2).strip()
            if candidate:
                self._add(candidate, source, context + " CSS url()", allow_no_ext=True, base_url=base_url)

    def _scan_embedded_text(self, value: str, source: str):
        normalized = value.replace(r"\/", "/")
        normalized = re.sub(r"\\u002f", "/", normalized, flags=re.I)
        normalized = re.sub(r"\\u0026", "&", normalized, flags=re.I)
        normalized = re.sub(r"\\u003d", "=", normalized, flags=re.I)
        for match in URL_RE.finditer(normalized):
            candidate = match.group(0).rstrip(".,;)]}")
            before = normalized[max(0, match.start() - 180):match.start()]
            key_match = re.search(
                r"([A-Za-z_$][\w$]*(?:sprite|poster|thumb|thumbnail|image|preview|cover)[\w$]*)\s*[\"']?\s*[:=]\s*[\"']?$",
                before,
                re.I,
            )
            key_hint = key_match.group(1) if key_match else ""
            self._add(candidate, source, key_hint or source, allow_no_ext=bool(key_hint))

    def _scan_json_fields(self, text_value: str, source: str):
        """Walk the initial player JSON for image URL fields such as spriteURL and poster."""
        raw = re.sub(r"^\s*(?:window\.)?initials\s*=\s*", "", text_value.strip(), count=1).strip()
        if raw.endswith(";"):
            raw = raw[:-1].strip()
        if raw.startswith("(") and raw.endswith(")"):
            raw = raw[1:-1].strip()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return

        field_hint = re.compile(r"(?:sprite|poster|thumb|thumbnail|image|preview|cover|icon|picture|favicon|background)", re.I)

        def walk(node, path: str = ""):
            if isinstance(node, dict):
                for key, value in node.items():
                    key_path = f"{path}.{key}" if path else str(key)
                    if isinstance(value, str) and field_hint.search(key_path):
                        self._add(value, source + " JSON:" + key_path, key_path, allow_no_ext=True)
                    if isinstance(value, (dict, list)):
                        walk(value, key_path)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    if isinstance(value, str) and field_hint.search(path):
                        self._add(value, source + f" JSON:{path}[{index}]", path, allow_no_ext=True)
                    elif isinstance(value, (dict, list)):
                        walk(value, f"{path}[{index}]")

        walk(payload)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {str(k).lower(): (v or "") for k, v in attrs if k}
        if tag == "head":
            self.section = "head"
        elif tag == "body":
            self.section = "body"
        elif tag == "picture":
            self.picture_depth += 1

        context_parts = [tag]
        if attr.get("id"):
            context_parts.append("id=" + attr["id"])
        if attr.get("class"):
            context_parts.append("class=" + attr["class"])
        context_parts.extend(value for key, value in attr.items() if any(
            word in key for word in ("poster", "sprite", "thumb", "thumbnail", "preview", "image", "cover")
        ))
        context = " ".join(context_parts)

        attr_names = {"src", "data-src", "data-lazy-src", "data-original", "data-original-src",
                      "data-image", "data-thumb", "data-thumbnail", "data-poster", "poster",
                      "data-sprite", "data-preview", "data-cover", "imagesrc"}
        for name in attr_names:
            if not attr.get(name):
                continue
            tag_allows = (
                (tag == "img" and name in {"src", "data-src", "data-lazy-src", "data-original",
                                           "data-original-src", "data-image", "data-thumb",
                                           "data-thumbnail", "data-preview", "data-cover"})
                or (tag == "video" and name == "poster")
                or name in {"data-poster", "data-sprite", "data-thumbnail", "data-thumb", "data-image", "data-preview", "data-cover", "imagesrc"}
                or (tag == "source" and name == "src" and (attr.get("type", "").startswith("image/") or self.picture_depth > 0))
            )
            if tag_allows:
                self._add(attr[name], f"<{tag}>@{name}", context, allow_no_ext=True)

        for name in ("srcset", "data-srcset", "imagesrcset"):
            if attr.get(name):
                self._srcset(attr[name], f"<{tag}>@{name}", context + " srcset")

        if attr.get("style"):
            self._css(attr["style"], f"<{tag}>@style", context + " inline style")

        if tag == "meta":
            prop = (attr.get("property") or attr.get("name") or attr.get("itemprop") or "").strip().lower()
            if attr.get("content") and (
                "image" in prop or prop in {"thumbnail", "thumbnailurl", "msapplication-tileimage"}
            ):
                self._add(attr["content"], "meta content:" + prop, prop + " meta image", allow_no_ext=True)

        if tag == "link" and attr.get("href"):
            rel = (attr.get("rel") or "").lower()
            rel_words = set(rel.split())
            if "stylesheet" in rel_words:
                sheet_url = normalize_url(attr["href"], self.page_url)
                if sheet_url and sheet_url not in self.stylesheets:
                    self.stylesheets.append(sheet_url)
            preload_image = "preload" in rel_words and (attr.get("as") or "").lower() == "image"
            image_link = preload_image or any(word in rel for word in ("icon", "image_src", "apple-touch"))
            if image_link:
                self._add(attr["href"], "link href:" + rel, rel + " " + attr.get("as", ""), allow_no_ext=True)

        if tag in {"script", "style"}:
            self.active_text_tag = tag
            self.active_text = []
            self.active_text_attrs = attr
            self.script_counter += 1

    def handle_data(self, data):
        if self.active_text_tag in {"script", "style"}:
            self.active_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"} and self.active_text_tag == tag:
            text_value = "".join(self.active_text)
            if tag == "style":
                self._css(text_value, "<style> block", "CSS/background")
            else:
                source = f"<script id={self.active_text_attrs.get('id') or self.script_counter}>"
                self._scan_embedded_text(text_value, source)
                if self.active_text_attrs.get("id") == "initials-script" or "videoModel" in text_value[:20000]:
                    self._scan_json_fields(text_value, source)
            self.active_text_tag = None
            self.active_text = []
            self.active_text_attrs = {}
        if tag == "picture":
            self.picture_depth = max(0, self.picture_depth - 1)
        if tag in {"head", "body"}:
            self.section = "document"

    def results(self) -> list[dict]:
        records = list(self.found.values())
        order = {"Video poster": 0, "Thumbnail / preview": 1, "Sprite sheet": 2,
                 "Social / metadata image": 3, "Preloaded image": 4, "Responsive image": 5,
                 "CSS / background image": 6, "Icon": 7, "Image": 8}
        return sorted(records, key=lambda item: (order.get(item["kind"], 99), item["url"]))


def make_html(page_url: str, final_url: str, status: int, response_bytes: int,
              assets: list[dict], response_chars: int, css_files_count: int) -> str:
    counts = Counter(item["kind"] for item in assets)
    sections = Counter()
    for item in assets:
        for section in {loc["section"] for loc in item["locations"]}:
            sections[section] += 1

    def e(value) -> str:
        return html.escape(str(value or ""), quote=True)

    cards = []
    for index, item in enumerate(assets, 1):
        sources = item["locations"]
        section_names = ", ".join(sorted({x["section"] for x in sources})) or "unknown"
        locations_html = "".join(
            f'<li><b>{e(x["section"])}</b> — {e(x["source"])} <span class="kind">{e(x["kind"])}</span></li>'
            for x in sources[:10]
        )
        original_html = "".join(f'<li><code>{e(v)}</code></li>' for v in item["original_urls"])
        url = e(item["url"])
        kind = e(item["kind"])
        cards.append(f"""
        <article class="asset-card">
          <div class="preview"><a href="{url}" target="_blank" rel="noopener noreferrer" title="Open rewritten asset">
            <img src="{url}" loading="lazy" alt="{kind} {index}" referrerpolicy="strict-origin-when-cross-origin"
                 onerror="this.classList.add('broken');this.alt='Preview failed to load — use Open asset to test URL';">
          </a></div>
          <div class="card-body">
            <div class="badges"><span class="badge">{kind}</span><span class="badge muted">{e(section_names)}</span></div>
            <h3>Asset {index}</h3>
            <p class="host">Original host: {e(item["source_host"])}</p>
            <p><a class="open" href="{url}" target="_blank" rel="noopener noreferrer">Open rewritten asset ↗</a></p>
            <details><summary>Found in markup</summary><ul>{locations_html}</ul></details>
            <details><summary>Original URL(s)</summary><ul>{original_html}</ul></details>
            <p class="url"><code>{url}</code></p>
          </div>
        </article>
        """)

    count_cards = "".join(
        f'<div class="stat"><strong>{count}</strong><span>{e(kind)}</span></div>'
        for kind, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ) or '<p>No image URLs matched the extraction rules.</p>'
    gallery = "\n".join(cards) or '<p class="empty">No images were found. Inspect the workflow logs.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Image extraction debug — {e(final_url)}</title>
<style>
:root{{color-scheme:dark;background:#0b1020;color:#e8edf7;font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
*{{box-sizing:border-box}}body{{margin:0}}a{{color:#91c8ff}}header{{padding:24px max(16px,calc((100% - 1380px)/2));background:#131b30;border-bottom:1px solid #2c3854}}
h1{{margin:0 0 8px;font-size:clamp(1.6rem,3vw,2.3rem)}}h2{{margin:28px 0 12px}}p{{overflow-wrap:anywhere}}.sub{{color:#b8c3d8;margin:6px 0}}
main{{max-width:1380px;margin:auto;padding:20px 16px 60px}}.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}}.stat{{background:#17213a;border:1px solid #2e3b59;border-radius:12px;padding:12px 16px;min-width:130px;display:flex;flex-direction:column}}.stat strong{{font-size:1.5rem}}.stat span{{font-size:.87rem;color:#b9c8dd}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,320px),1fr));gap:16px}}.asset-card{{border:1px solid #2b3650;border-radius:14px;overflow:hidden;background:#11192b;min-width:0}}.preview{{background:#080c16;min-height:120px;display:flex;align-items:center;justify-content:center}}.preview a{{display:block;width:100%}}.preview img{{display:block;max-width:100%;width:100%;height:230px;object-fit:contain}}.preview img.broken{{min-height:90px;object-fit:contain}}.card-body{{padding:14px}}h3{{margin:9px 0 2px}}.host{{font-size:.84rem;color:#aab9d1}}.badges{{display:flex;flex-wrap:wrap;gap:6px}}.badge{{background:#1e3b5d;color:#d8edff;border-radius:999px;padding:3px 9px;font-size:.75rem}}.badge.muted{{background:#283044;color:#cad2e1}}.open{{display:inline-block;background:#1a4e7a;color:white;border-radius:8px;padding:7px 10px;text-decoration:none}}details{{margin:10px 0}}summary{{cursor:pointer;color:#bed6f3}}li,code{{overflow-wrap:anywhere;word-break:break-word}}code{{font-size:.82rem;color:#d5deef}}.url{{border-top:1px solid #29364f;padding-top:9px}}.empty{{padding:24px;background:#17213a;border-radius:12px}}
</style>
</head>
<body>
<header>
  <h1>Image extraction debug gallery</h1>
  <p class="sub">Target: <a href="{e(page_url)}" target="_blank" rel="noopener noreferrer">{e(page_url)}</a></p>
  <p class="sub">Final page: {e(final_url)} · HTTP {status} · {response_bytes:,} bytes · {response_chars:,} decoded characters</p>
  <p class="sub">Image CDN rewrite target: <code>https://{CDN_HOST}/</code>. External stylesheets scanned: {css_files_count}. Each thumbnail is clickable.</p>
</header>
<main>
  <h2>Extraction summary</h2>
  <div class="stats">
    <div class="stat"><strong>{len(assets)}</strong><span>Unique image assets</span></div>
    <div class="stat"><strong>{sections.get("head", 0)}</strong><span>Found in head</span></div>
    <div class="stat"><strong>{sections.get("body", 0)}</strong><span>Found in body</span></div>
    <div class="stat"><strong>{sections.get("document", 0)}</strong><span>Other / embedded context</span></div>
  </div>
  <div class="stats">{count_cards}</div>
  <p class="sub">Scans image/lazy attributes, srcset, social metadata, preload/icon links, video poster attributes, inline CSS background URLs, style blocks, and image URLs embedded in inline scripts/player JSON. External CSS files and JavaScript-loaded network calls are not fetched.</p>
  <h2>Assets</h2>
  <div class="grid">{gallery}</div>
</main>
</body>
</html>
"""


def extract(page_path: str, base_url: str) -> tuple[str, str, int, int, int, list[dict], int]:
    from resolver import _read_source_page, source_page_url
    from extractor import http_session

    if not page_path.startswith("/videos/") or page_path.startswith("//"):
        raise ValueError("PAGE_PATH must start with /videos/ and must be a path only.")
    page_url = source_page_url(page_path, base_url)
    source_host = (urlsplit(base_url).hostname or "").lower()
    final_url, body = _read_source_page(page_url, source_host)
    lowered = body.lower()
    title_match = re.search(r"<title\\b[^>]*>(.*?)</title\\s*>", body, re.I | re.S)
    page_title = html.unescape(re.sub(r"\\s+", " ", title_match.group(1))).strip()[:160] if title_match else "(no title)"
    age_markers = (
        "verify your age", "age verification", "confirm your age",
        "age-check", "age check", "parental-control", "parental control",
        "must be 18", "i am 18", "are you 18",
    )
    present_markers = [marker for marker in age_markers if marker in lowered]
    logging.getLogger("image-debug").info(
        "Source page shape: title=%r; initials_script=%s; videoModel=%s; age_gate_markers=%s",
        page_title,
        bool(re.search(r'<script\\b[^>]*\\bid=["\\']initials-script["\\']', body, re.I)),
        "present" if "videomodel" in lowered else "absent",
        ",".join(present_markers) or "none",
    )
    parser = AssetExtractor(final_url)
    parser.feed(body)
    parser.close()

    # Fetch a bounded number of linked source-site stylesheets to discover CSS
    # background images and sprite sheets. Never follow redirects for CSS requests.
    session = http_session()
    total_css_bytes = 0
    seen_css_urls = set()
    for sheet_url in parser.stylesheets[:8]:
        parsed = urlsplit(sheet_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password:
            continue
        if host != source_host and not host.endswith(".xhcdn.com"):
            continue
        if sheet_url in seen_css_urls:
            continue
        seen_css_urls.add(sheet_url)
        if total_css_bytes >= 1_500_000:
            break
        try:
            response = session.get(
                sheet_url,
                headers={"Accept": "text/css,*/*;q=0.8"},
                timeout=(3, 6),
                allow_redirects=False,
                stream=True,
            )
        except Exception:
            continue
        try:
            if response.status_code != 200:
                continue
            body_bytes = bytearray()
            per_sheet_limit = min(250_000, 1_500_000 - total_css_bytes)
            for chunk in response.iter_content(16384):
                if not chunk:
                    continue
                remaining = per_sheet_limit - len(body_bytes)
                if remaining <= 0:
                    break
                body_bytes.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    break
            if not body_bytes:
                continue
            css_text = bytes(body_bytes).decode(response.encoding or "utf-8", errors="replace")
            total_css_bytes += len(body_bytes)
            parser._css(css_text, "external stylesheet " + parsed.path, "external CSS/background", base_url=sheet_url)
            parser.external_css_count += 1
        except Exception:
            continue
        finally:
            response.close()
    return page_url, final_url, 200, len(body.encode("utf-8")), len(body), parser.results(), parser.external_css_count


def self_test() -> int:
    page_url = "https://mobilexh.live/videos/1193895"
    sample = r"""
    <html><head>
      <meta property="og:image" content="https://ic-vt-nss.xhcdn.com/a/cover.webp">
      <link rel="preload" as="image" href="https://ic-vrm-nss.xhcdn.com/a/hero.jpg">
      <style>.sprite{background-image:url('https://ic-vt-nss.xhcdn.com/a/sprite.jpg')}</style>
    </head><body>
      <img src="https://thumb-v8.xhcdn.com/a/thumb.webp" data-srcset="https://thumb-v8.xhcdn.com/a/thumb.webp 1x">
      <video poster="https://ic-vt-nss.xhcdn.com/a/poster.webp"></video>
      <div style="background-image:url(/a/bg.jpg)"></div>
      <script>window.initials={"videoModel":{"spriteURL":"https:\/\/ic-vrm-nss.xhcdn.com\/a\/player-sprite.webp","thumbnailURL":"https://ic-vt-nss.xhcdn.com/a/player-thumb.webp"}};</script>
    </body></html>
    """
    parser = AssetExtractor(page_url)
    parser.feed(sample)
    parser.close()
    assets = parser.results()
    urls = {asset["url"] for asset in assets}
    kinds = {asset["kind"] for asset in assets}
    assert any(url.startswith("https://" + CDN_HOST + "/a/cover.webp") for url in urls), urls
    assert any(url.startswith("https://" + CDN_HOST + "/a/poster.webp") for url in urls), urls
    assert "Video poster" in kinds, kinds
    assert "Sprite sheet" in kinds, kinds
    assert "Thumbnail / preview" in kinds, kinds
    assert any(loc["section"] == "head" for asset in assets for loc in asset["locations"])
    assert any(loc["section"] == "body" for asset in assets for loc in asset["locations"])
    print(f"Offline self-test passed: {len(assets)} unique image assets; kinds={sorted(kinds)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="", help="Source path such as /videos/1193895")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser tests only")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    page_path = (args.path or os.environ.get("PAGE_PATH") or DEFAULT_PATH).strip()
    base_url = os.environ.get("SITE_BASE_URL", "https://mobilexh.live").strip().rstrip("/")
    try:
        page_url, final_url, status, size, chars, assets, css_files_count = extract(page_path, base_url)
    except Exception as exc:
        print(f"Image extraction failed: {type(exc).__name__}: {str(exc)[:400]}")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(make_html(page_url, final_url, status, size, assets, chars, css_files_count), encoding="utf-8")
    counts = Counter(item["kind"] for item in assets)
    print(json.dumps({
        "target": page_url,
        "final_url": final_url,
        "output": str(OUTPUT.relative_to(ROOT)),
        "http_status": status,
        "source_bytes": size,
        "source_characters": chars,
        "unique_images": len(assets),
        "image_cdn_rewrite_host": CDN_HOST,
        "external_css_files_scanned": css_files_count,
        "kinds": dict(counts),
        "html_bytes": OUTPUT.stat().st_size,
        "artifact_upload": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
