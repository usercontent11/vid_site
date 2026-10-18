"""Resolve an M3U8 URL from the configured public video source page."""
import html
import ipaddress
import re
from urllib.parse import quote, urljoin, urlsplit

import requests

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


def source_page_url(page_path: str, source_base_url: str) -> str:
    """Build a source URL from a stored, path-only videos URL."""
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


def validate_manifest(value: str, page_url: str) -> str | None:
    """Accept an HTTPS M3U8 URL without maintaining a static CDN hostname list."""
    candidate = html.unescape(value.strip()).rstrip(";,)]}")
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
        or not parsed.path.lower().endswith(".m3u8")
    ):
        return None

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".local", ".internal", ".test", ".invalid")
    ):
        return None
    try:
        ip = ipaddress.ip_address(hostname)
        if not ip.is_global:
            return None
    except ValueError:
        if "." not in hostname:
            return None
    return parsed.geturl()


def extract_m3u8_url(page_html: str, page_url: str) -> str:
    """Extract any valid public HTTPS M3U8 URL found inside source HTML."""
    if not isinstance(page_html, str) or not page_html:
        raise StreamResolutionError("The source page was empty.")

    normalized = html.unescape(page_html)
    normalized = (
        normalized.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u002f", "/")
    )
    candidates = PATTERN.findall(normalized)
    for candidate in candidates:
        manifest = validate_manifest(candidate, page_url)
        if manifest:
            return manifest
    if candidates:
        raise StreamResolutionError("An M3U8 link was found but was not a valid public HTTPS manifest.")
    raise StreamResolutionError("No M3U8 manifest was found in the source page.")


def _read_source_page(url: str, source_host: str) -> tuple[str, str]:
    """Fetch the source page, allowing at most one same-host HTTPS redirect."""
    current = url
    for attempt in range(2):
        try:
            response = requests.get(
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
    """Resolve from the configured source page; never persist the M3U8 URL."""
    page_url = source_page_url(page_path, source_base_url)
    source_host = (urlsplit(source_base_url).hostname or "").lower()
    final_url, page_html = _read_source_page(page_url, source_host)
    return extract_m3u8_url(page_html, final_url)
