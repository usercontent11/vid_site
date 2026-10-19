"""Resolve HLS from the source page's player-preload link or embedded HTML."""
import html
import ipaddress
import json
import os
import re
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from extractor import http_session

PATTERN = re.compile(
    r"""(?i)(https?://[^\s"'<>]+?\.m3u8(?:\?[^\s"'<>]*)?|/[^\s"'<>]+?\.m3u8(?:\?[^\s"'<>]*)?)"""
)
MAX_BYTES = 4 * 1024 * 1024
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VideoCatalogue/1.0)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.8",
}


class StreamResolutionError(RuntimeError):
    """The source page did not produce a safe public HLS manifest."""


class _HlsPreloadParser(HTMLParser):
    """Find the source site's documented preload hint for its HLS master playlist."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        values = {key.lower(): value for key, value in attrs if key}
        rel = set((values.get("rel") or "").lower().split())
        resource_type = (values.get("as") or "").lower()
        # XHamster embeds its master playlist in a fetch preload link.
        if "preload" in rel and resource_type == "fetch" and "crossorigin" in values:
            href = values.get("href")
            if href:
                self.hrefs.append(href.strip())


def source_page_url(page_path: str, source_base_url: str) -> str:
    """Build a source URL from a stored, path-only /videos/... URL."""
    if not isinstance(page_path, str) or not page_path.strip():
        raise StreamResolutionError("Missing video page path.")
    path = urlsplit(page_path.strip())
    if path.scheme or path.netloc or path.query or path.fragment:
        raise StreamResolutionError("Video page path must be path-only.")
    if not path.path.startswith("/videos/") or path.path.startswith("//"):
        raise StreamResolutionError("Only stored /videos/... paths can be resolved.")

    base = urlsplit(source_base_url)
    if base.scheme != "https" or not base.hostname or base.username or base.password:
        raise StreamResolutionError("SOURCE_BASE_URL must be a valid HTTPS origin.")
    safe_path = quote(path.path, safe="/%:@!$&'()*+,;=-._~")
    result = urljoin(source_base_url.rstrip("/") + "/", safe_path.lstrip("/"))
    parsed = urlsplit(result)
    if parsed.scheme != "https" or parsed.hostname != base.hostname:
        raise StreamResolutionError("The source URL escaped its configured host.")
    return result


def _normalize_url_text(value: str) -> str:
    value = html.unescape(value.strip())
    # The source serializes slashes both as escaped slashes and as JSON Unicode escapes.
    value = value.replace("\\/", "/")
    value = re.sub(r"\\u002[fF]", "/", value)
    value = re.sub(r"\\u0026", "&", value)
    value = re.sub(r"\\u003[dD]", "=", value)
    return value


def validate_public_https_url(value: str, page_url: str) -> str | None:
    """Validate an HTTPS URL without assuming the stream host or filename extension."""
    candidate = _normalize_url_text(value)
    if not candidate:
        return None
    try:
        parsed = urlsplit(urljoin(page_url, candidate))
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return None

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".local", ".internal", ".test", ".invalid")
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
        if not address.is_global:
            return None
    except ValueError:
        if "." not in hostname:
            return None
    return parsed.geturl()


def validate_manifest(value: str, page_url: str) -> str | None:
    """Validate a direct manifest URL whose path ends in .m3u8."""
    normalized = _normalize_url_text(value).rstrip(";,)]}")
    result = validate_public_https_url(normalized, page_url)
    if not result:
        return None
    if not urlsplit(result).path.lower().endswith(".m3u8"):
        return None
    return result


def extract_m3u8_url(page_html: str, page_url: str) -> str:
    """Get the HLS URL from the player's preload hint, then fall back to URL scanning."""
    if not isinstance(page_html, str) or not page_html:
        raise StreamResolutionError("The source page was empty.")

    # This is how the source player exposes its master playlist. Do this before
    # scanning text for a literal .m3u8 extension: some playlist URLs have no suffix.
    preload_parser = _HlsPreloadParser()
    try:
        preload_parser.feed(page_html)
        preload_parser.close()
    except Exception:
        pass
    for href in preload_parser.hrefs:
        candidate = validate_public_https_url(href, page_url)
        if candidate:
            return candidate

    normalized = _normalize_url_text(page_html)
    candidates = PATTERN.findall(normalized)
    for candidate in candidates:
        manifest = validate_manifest(candidate, page_url)
        if manifest:
            return manifest
    if candidates:
        raise StreamResolutionError("An M3U8 link was found but was not a valid public HTTPS manifest.")

    # Log structure only: never raw HTML, URLs, cookies, or tokens.
    title_match = re.search(r"<title[^>]*>(.*?)</title\s*>", page_html, re.I | re.S)
    page_title = (
        re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:100]
        if title_match else "missing"
    )
    html_chars = len(page_html)
    script_match = re.search(
        r'<script\b[^>]*\bid=["\']initials-script["\'][^>]*>(.*?)</script\s*>',
        page_html,
        re.I | re.S,
    )
    initial_keys = []
    initial_data = None
    parse_state = "missing"
    if script_match:
        parse_state = "unparseable"
        try:
            raw = re.sub(
                r"^\s*window\.initials\s*=\s*",
                "",
                script_match.group(1).strip(),
                count=1,
            ).strip()
            if raw.endswith(";"):
                raw = raw[:-1]
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                initial_data = parsed
                initial_keys = sorted(str(k) for k in parsed.keys())
                parse_state = "parsed"
        except (TypeError, ValueError):
            pass

    model_present = isinstance(initial_data, dict) and "videoModel" in initial_data
    model = initial_data.get("videoModel") if isinstance(initial_data, dict) else None
    model_type = type(model).__name__ if model_present else "missing"
    model_keys = sorted(str(k) for k in model.keys())[:40] if isinstance(model, dict) else []

    placeholder = initial_data.get("avPlaceholderComponent") if isinstance(initial_data, dict) else None
    placeholder_title = placeholder.get("title", "") if isinstance(placeholder, dict) else ""
    placeholder_text = re.sub(r"<[^>]*>", " ", str(placeholder_title)).lower()
    age_verification_prompt = (
        "verify your age" in placeholder_text and "logged in" in placeholder_text
    )

    download_component = (
        initial_data.get("downloadDropdownComponent") if isinstance(initial_data, dict) else None
    )
    component_sources = (
        download_component.get("sources") if isinstance(download_component, dict) else None
    )
    source_types = sorted(str(k) for k in component_sources.keys())[:20] if isinstance(component_sources, dict) else []

    model_summary = (
        f"initials_json={parse_state}, initial_keys_count={len(initial_keys)}, "
        f"videoModel_present={model_present}, videoModel_type={model_type}, "
        f"videoModel_keys={model_keys!r}, age_verification_prompt={age_verification_prompt}, "
        f"downloadDropdown_sources_present={isinstance(component_sources, dict)}, "
        f"source_type_keys={source_types!r}"
    )
    raise StreamResolutionError(
        f"No M3U8 manifest was found; page_title={page_title!r}, html_chars={html_chars}, "
        f"hls_preload_link={'present_but_invalid' if preload_parser.hrefs else 'missing'}, "
        f"{model_summary}. The source may serve a different page variant or require age verification/login."
    )



def extract_download_file_url(page_html: str, page_url: str) -> str | None:
    """Read videoModel.downloadFile only as a constrained same-site file fallback."""
    script_match = re.search(
        r'<script\b[^>]*\bid=["\']initials-script["\'][^>]*>(.*?)</script\s*>',
        page_html,
        re.I | re.S,
    )
    if not script_match:
        return None
    try:
        raw = re.sub(
            r"^\s*window\.initials\s*=\s*",
            "",
            script_match.group(1).strip(),
            count=1,
        ).strip()
        if raw.endswith(";"):
            raw = raw[:-1]
        initial_data = json.loads(raw)
    except (TypeError, ValueError):
        return None

    model = initial_data.get("videoModel") if isinstance(initial_data, dict) else None
    download_file = model.get("downloadFile") if isinstance(model, dict) else None
    if not isinstance(download_file, str) or not download_file.strip():
        return None

    candidate = validate_public_https_url(_normalize_url_text(download_file), page_url)
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    page_host = (urlsplit(page_url).hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None

    # Do not accept arbitrary hosts or general URLs from page JSON. The only
    # fallback allowed here is the source site's own movie-download route.
    if (
        (parsed.hostname or "").lower().rstrip(".") != page_host
        or port not in (None, 443)
        or not parsed.path.lower().startswith("/movies/")
        or "/download/" not in parsed.path.lower()
    ):
        return None
    return candidate


def _read_source_page(url: str, source_host: str) -> tuple[str, str]:
    """Fetch the source page, allowing at most one same-host HTTPS redirect."""
    current = url
    for attempt in range(2):
        try:
            # Reuse the same source session/cookies as the metadata extractor.
            response = http_session().get(
                current,
                headers=HEADERS,
                timeout=(4, 12),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise StreamResolutionError("The source page request failed.") from exc

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            response.close()
            if attempt > 0 or not location:
                raise StreamResolutionError("The source page redirected more than once.")
            destination = urlsplit(urljoin(current, location))
            try:
                port = destination.port
            except ValueError:
                port = -1
            if (
                destination.scheme != "https"
                or (destination.hostname or "").lower() != source_host
                or port not in (None, 443)
                or destination.username
                or destination.password
            ):
                raise StreamResolutionError("The source page redirected to an unapproved host.")
            current = destination.geturl()
            continue

        try:
            if response.status_code in (401, 403, 404):
                raise StreamResolutionError("The source video page is not publicly accessible.")
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                remaining = MAX_BYTES - len(data)
                if remaining <= 0:
                    break
                data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    break
            body = bytes(data).decode(response.encoding or "utf-8", errors="replace")
            return current, body
        except requests.RequestException as exc:
            raise StreamResolutionError("The source page could not be read.") from exc
        finally:
            response.close()
    raise StreamResolutionError("Could not load the source page.")


def resolve_stream_url(page_path: str, source_base_url: str) -> str:
    """Resolve from the configured source page; never persist the HLS URL."""
    page_url = source_page_url(page_path, source_base_url)
    source_host = (urlsplit(source_base_url).hostname or "").lower()
    final_url, page_html = _read_source_page(page_url, source_host)
    return extract_m3u8_url(page_html, final_url)



DEFAULT_NEON_VIDEO_EXTRACTOR_URL = "https://br-super-sound-b5ty7f80-videodebug.compute.c-7.us-east-2.aws.neon.tech/"


def _rewrite_video_cdn_host(value: str) -> str:
    """Map the source's video-nss host to the requested CDN hostname."""
    parsed = urlsplit(value)
    if (parsed.hostname or "").lower().rstrip(".") != "video-nss.xhcdn.com":
        return value
    netloc = "video-nss.cdnsolutions.media"
    if parsed.port and parsed.port != 443:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _resolve_from_neon_debug(page_path: str, source_base_url: str) -> tuple[str, str]:
    """Ask the deployed Neon extractor for all media URLs and choose a playable one."""
    page_url = source_page_url(page_path, source_base_url)
    endpoint = os.getenv("NEON_VIDEO_EXTRACTOR_URL", DEFAULT_NEON_VIDEO_EXTRACTOR_URL).strip()
    try:
        endpoint_parts = urlsplit(endpoint)
        if endpoint_parts.scheme != "https" or not endpoint_parts.hostname or endpoint_parts.username or endpoint_parts.password:
            raise StreamResolutionError("The Neon video extractor endpoint must be a valid HTTPS URL.")
        response = requests.get(
            endpoint,
            params={"url": page_url},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; VideoCatalogue/1.0)"},
            timeout=(4, 18),
        )
        response.raise_for_status()
        payload = response.json()
    except StreamResolutionError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise StreamResolutionError("The Neon video extractor request failed.") from exc

    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise StreamResolutionError("The Neon video extractor could not inspect the source page.")
    raw_links = payload.get("links")
    if not isinstance(raw_links, list):
        raise StreamResolutionError("The Neon video extractor returned no media-link list.")

    manifests = []
    media_files = []
    for raw in raw_links:
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = validate_public_https_url(raw, page_url)
        if not candidate:
            continue
        path = urlsplit(candidate).path.lower()
        if path.endswith(".m3u8"):
            manifest = validate_manifest(candidate, page_url)
            if manifest:
                manifests.append(manifest)
            continue
        if not path.endswith((".mp4", ".m4v", ".webm", ".mpd")):
            continue
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        # Preview clips are often .mp4 files too; do not mistake them for the full video.
        if hostname.startswith("thumb-v") or path.endswith(".t.mp4") or "/526x298" in path:
            continue
        media_files.append(candidate)

    # Prefer HLS whenever available. The page may contain many unrelated preview clips.
    if manifests:
        preferred = next(
            (u for u in manifests if (urlsplit(u).hostname or "").lower() == "video-nss.xhcdn.com"),
            manifests[0],
        )
        return _rewrite_video_cdn_host(preferred), "hls"

    if media_files:
        # Prefer explicit quality files (e.g. /240p.h264.mp4) over template-style URLs.
        def media_score(value: str) -> tuple[int, int]:
            path = urlsplit(value).path.lower()
            quality_match = re.search(r"/\d{3,4}p(?:\.h264)?\.mp4$", path)
            return (0 if quality_match else 1, len(path))

        media_files.sort(key=media_score)
        return _rewrite_video_cdn_host(media_files[0]), "file"

    raise StreamResolutionError("The Neon video extractor did not find a usable HLS manifest or video file.")


def resolve_stream_source(page_path: str, source_base_url: str) -> tuple[str, str]:
    """Prefer URLs from Neon Functions; fall back to the existing page parser."""
    try:
        return _resolve_from_neon_debug(page_path, source_base_url)
    except StreamResolutionError:
        # Keep the original resolver as a fallback if the Neon function is unavailable
        # or a page variant contains a source hint the debug scanner missed.
        pass

    page_url = source_page_url(page_path, source_base_url)
    source_host = (urlsplit(source_base_url).hostname or "").lower()
    final_url, page_html = _read_source_page(page_url, source_host)

    try:
        stream_url = extract_m3u8_url(page_html, final_url)
        return _rewrite_video_cdn_host(stream_url), "hls"
    except StreamResolutionError as manifest_error:
        download_url = extract_download_file_url(page_html, final_url)
        if download_url:
            return _rewrite_video_cdn_host(download_url), "file"
        raise manifest_error



def _safe_url_summary(value: str | None, page_url: str) -> dict | None:
    """Summarize URLs without returning query values, which may contain signed tokens."""
    if not value:
        return None
    from urllib.parse import parse_qsl

    try:
        absolute = urljoin(page_url, html.unescape(value.strip()))
        parsed = urlsplit(absolute)
        query_names = sorted({name for name, _value in parse_qsl(parsed.query, keep_blank_values=True)})
        return {
            "scheme": parsed.scheme,
            "host": (parsed.hostname or "")[:160],
            "path": parsed.path[:320],
            "has_query": bool(parsed.query),
            "query_parameter_names": query_names[:40],
            "has_fragment": bool(parsed.fragment),
        }
    except Exception:
        return {"parse_error": True}


class _PageStructureParser(HTMLParser):
    """Collect markup structure and player-related hints without retaining values in output."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.title_parts = []
        self.in_title = False
        self.current_script = None
        self.scripts = []
        self.links = []
        self.media_elements = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {key.lower(): value for key, value in attrs if key}
        if tag == "title":
            self.in_title = True
        if tag == "script":
            self.current_script = {
                "id": (attr.get("id") or "")[:120],
                "type": (attr.get("type") or "")[:100],
                "src": _safe_url_summary(attr.get("src"), self.page_url),
                "chars": 0,
                "contains": set(),
                "_text": [],
            }
        elif tag == "link":
            rel = (attr.get("rel") or "").lower().split()
            self.links.append({
                "rel": rel[:12],
                "as": (attr.get("as") or "")[:60],
                "type": (attr.get("type") or "")[:100],
                "crossorigin_attribute_present": "crossorigin" in attr,
                "href": _safe_url_summary(attr.get("href"), self.page_url),
            })
        elif tag in {"video", "source", "iframe", "object", "embed"}:
            self.media_elements.append({
                "tag": tag,
                "type": (attr.get("type") or "")[:100],
                "src": _safe_url_summary(attr.get("src"), self.page_url),
                "data_url": _safe_url_summary(attr.get("data"), self.page_url),
                "attribute_names": sorted(k for k in attr if k not in {"src", "data"})[:60],
            })

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.current_script is not None:
            text_value = "".join(self.current_script.pop("_text", []))
            lowered = text_value.lower()
            for term in ("m3u8", "videoModel", "videoDescription", "player", "playlist",
                         "hls", "master", "sources", "videoUrl", "video_url", "stream", "media"):
                if term.lower() in lowered:
                    self.current_script["contains"].add(term)
            self.current_script["contains"] = sorted(self.current_script["contains"])
            self.current_script["chars"] = len(text_value)
            self.current_script["id"] = self.current_script["id"] or None
            self.current_script["type"] = self.current_script["type"] or None
            self.current_script["json"] = None
            stripped = text_value.strip()
            if self.current_script["id"] == "initials-script":
                raw = re.sub(r"^\s*window\.initials\s*=\s*", "", stripped, count=1).strip()
                if raw.endswith(";"):
                    raw = raw[:-1]
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        model = parsed.get("videoModel")
                        self.current_script["json"] = {
                            "top_level_keys": sorted(str(k) for k in parsed.keys())[:100],
                            "videoModel_type": type(model).__name__ if "videoModel" in parsed else "missing",
                            "videoModel_keys": sorted(str(k) for k in model.keys())[:100] if isinstance(model, dict) else [],
                            "interesting_key_paths": _interesting_json_key_paths(parsed),
                        }
                except Exception:
                    self.current_script["json"] = {"parse_error": True}
            elif ("json" in (self.current_script["type"] or "").lower() or stripped.startswith(("{", "["))):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, (dict, list)):
                        self.current_script["json"] = {
                            "top_level_type": type(parsed).__name__,
                            "interesting_key_paths": _interesting_json_key_paths(parsed),
                        }
                except Exception:
                    pass
            if self.current_script["id"] == "appcontext-script":
                # Report object-property identifiers only; never return URL or token values.
                self.current_script["javascript_structure"] = _javascript_structure_summary(text_value)
            self.scripts.append(self.current_script)
            self.current_script = None

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.current_script is not None:
            self.current_script["_text"].append(data)


def _interesting_json_key_paths(value, prefix="$", depth=0, budget=None):
    """Return JSON key names/path only, never field values; bound traversal for large scripts."""
    if budget is None:
        budget = [0]
    if depth > 7 or budget[0] >= 500:
        return []
    found = []
    if isinstance(value, dict):
        for key, child in list(value.items())[:200]:
            budget[0] += 1
            key_text = str(key)[:100]
            path = prefix + "." + key_text
            if re.search(r"(video|player|stream|source|hls|m3u8|media|file|playlist|quality|master|trailer|url|model|manifest|preload|dash|mp4)", key_text, re.I):
                found.append({"path": path, "value_type": type(child).__name__})
            if isinstance(child, (dict, list)):
                found.extend(_interesting_json_key_paths(child, path, depth + 1, budget))
            if budget[0] >= 500:
                break
    elif isinstance(value, list):
        for idx, child in enumerate(value[:80]):
            if isinstance(child, (dict, list)):
                found.extend(_interesting_json_key_paths(child, f"{prefix}[{idx}]", depth + 1, budget))
            if budget[0] >= 500:
                break
    return found[:150]


def _javascript_structure_summary(script_text: str) -> dict:
    """Summarize JavaScript object property identifiers without returning values."""
    key_pattern = re.compile(
        r"""(?:"([^"\\]{1,100})"|'([^'\\]{1,100})'|([A-Za-z_$][\w$-]{0,99}))\s*:"""
    )
    names = set()
    for match in key_pattern.finditer(script_text or ""):
        name = next((part for part in match.groups() if part is not None), "")
        if name and not name.startswith(("http:", "https:")):
            names.add(name)
        if len(names) >= 400:
            break
    interesting = sorted(
        name for name in names
        if re.search(r"(video|player|stream|source|hls|m3u8|media|file|playlist|quality|master|trailer|url|model|manifest|preload|dash|mp4|id|token)", name, re.I)
    )
    return {
        "property_name_count": len(names),
        "property_names": sorted(names)[:220],
        "player_related_property_names": interesting[:160],
        "values_returned": False,
    }


def inspect_page_structure(page_html: str, page_url: str) -> dict:
    """Build a safe JSON structure report for the source page; redact URL query values."""
    parser = _PageStructureParser(page_url)
    parser.feed(page_html or "")
    parser.close()
    text_lower = (page_html or "").lower()
    terms = ("m3u8", "videomodel", "videodescription", "player", "playlist", "hls", "master",
             "sources", "videourl", "stream", "manifest", "application/json")
    keyword_counts = {term: text_lower.count(term.lower()) for term in terms}
    links = parser.links[:100]
    scripts = parser.scripts[:100]
    return {
        "page": {
            "title": re.sub(r"\s+", " ", html.unescape(" ".join(parser.title_parts))).strip()[:200],
            "url": _safe_url_summary(page_url, page_url),
            "html_characters": len(page_html or ""),
            "has_initials_script": any(s.get("id") == "initials-script" for s in scripts),
        },
        "keyword_counts": keyword_counts,
        "link_count": len(parser.links),
        "links": links,
        "script_count": len(parser.scripts),
        "scripts": scripts,
        "media_element_count": len(parser.media_elements),
        "media_elements": parser.media_elements[:60],
    }


def inspect_source_page_structure(page_path: str, source_base_url: str) -> dict:
    """Fetch the configured source page and return a redacted structural report."""
    page_url = source_page_url(page_path, source_base_url)
    source_host = (urlsplit(source_base_url).hostname or "").lower()
    final_url, body = _read_source_page(page_url, source_host)
    report = inspect_page_structure(body, final_url)
    report["fetch"] = {"ok": True, "redirected_within_source_host": final_url != page_url}
    return report
