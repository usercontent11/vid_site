"""Resolve HLS from the source page's player-preload link or embedded HTML."""
import html
import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

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

    # Log page structure only; never raw HTML, cookies, stream URLs, or tokens.
    title_match = re.search(r"<title[^>]*>(.*?)</title\s*>", page_html, re.I | re.S)
    page_title = (
        re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:100]
        if title_match else "missing"
    )
    script_match = re.search(
        r'<script\b[^>]*\bid=["\']initials-script["\'][^>]*>(.*?)</script\s*>',
        page_html,
        re.I | re.S,
    )
    model_info = "initials_script=missing"
    if script_match:
        model_info = "initials_script=present"
        try:
            raw = re.sub(r"^\s*window\.initials\s*=\s*", "", script_match.group(1).strip(), count=1).strip()
            if raw.endswith(";"):
                raw = raw[:-1]
            initial_data = json.loads(raw)
            if isinstance(initial_data, dict):
                top_keys = sorted(str(k) for k in initial_data.keys())[:30]
                model = initial_data.get("videoModel")
                model_keys = sorted(str(k) for k in model.keys())[:30] if isinstance(model, dict) else []
                model_info += f", initial_keys={top_keys!r}, videoModel_keys={model_keys!r}"
        except Exception:
            model_info += ", initials_json=unparseable"
    raise StreamResolutionError(
        f"No M3U8 manifest was found; page_title={page_title!r}, "
        f"hls_preload_link={'present_but_invalid' if preload_parser.hrefs else 'missing'}, {model_info}. "
        "The source may have changed its player markup or may require a separate player/API request."
    )


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
