"""Allowlisted resolver for public HLS manifests found on a video page."""
import html, ipaddress, os, re
from urllib.parse import quote, urljoin, urlsplit
import requests

PATTERN=re.compile(r"""(?i)(https?://[^\s"'<>]+?\.m3u8(?:\?[^\s"'<>]*)?|/[^\s"'<>]+?\.m3u8(?:\?[^\s"'<>]*)?)""")
MAX_BYTES=4*1024*1024

class StreamResolutionError(RuntimeError): pass

def source_page_url(page_path, source_base_url):
    if not isinstance(page_path,str) or not page_path.strip():raise StreamResolutionError("Missing video page path.")
    path=urlsplit(page_path.strip())
    if path.scheme or path.netloc or path.query or path.fragment or not path.path.startswith("/videos/") or path.path.startswith("//"):
        raise StreamResolutionError("Only stored /videos/... paths are allowed.")
    base=urlsplit(source_base_url)
    if base.scheme!="https" or not base.hostname:raise StreamResolutionError("SOURCE_BASE_URL must be HTTPS.")
    safe_path=quote(path.path,safe="/%:@!$&'()*+,;=-._~")
    url=urljoin(source_base_url.rstrip("/")+"/",safe_path.lstrip("/"))
    parsed=urlsplit(url)
    if parsed.scheme!="https" or parsed.hostname!=base.hostname:raise StreamResolutionError("Source URL escaped its configured host.")
    return url

def host_allowlist(source_base_url, configured=None):
    source=(urlsplit(source_base_url).hostname or "").lower()
    configured = configured if configured is not None else os.getenv("STREAM_ALLOWED_HOSTS","cdnsolutions.media").split(",")
    hosts={source} if source else set()
    hosts.update(str(x).strip().lower().lstrip(".") for x in configured if str(x).strip())
    return hosts

def allowed(host, hosts):
    host=host.lower().rstrip(".")
    return any(host==x or host.endswith("."+x) for x in hosts if x)

def validate_manifest(value, page_url, hosts):
    value=html.unescape(value.strip()).rstrip(";,)]}")
    parsed=urlsplit(urljoin(page_url,value))
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):return None
    host=parsed.hostname.lower().rstrip(".")
    try:
        ip=ipaddress.ip_address(host)
        if not ip.is_global:return None
    except ValueError:pass
    if not allowed(host,hosts) or not parsed.path.lower().endswith(".m3u8"):return None
    return parsed.geturl()

def extract_m3u8_url(page_html, page_url, allowed_hosts=None):
    if not isinstance(page_html,str) or not page_html:raise StreamResolutionError("The source page was empty.")
    source=(urlsplit(page_url).hostname or "").lower()
    hosts=set(allowed_hosts or {source,"cdnsolutions.media"})
    normalized=html.unescape(page_html).replace("\\/","/").replace("\\u0026","&").replace("\\u003d","=").replace("\\u002f","/")
    candidates=PATTERN.findall(normalized)
    for candidate in candidates:
        result=validate_manifest(candidate,page_url,hosts)
        if result:return result
    if candidates:raise StreamResolutionError("M3U8 found but its host was not allowlisted.")
    raise StreamResolutionError("No M3U8 manifest was found.")

def resolve_stream_url(page_path, source_base_url, allowed_hosts=None):
    """Fetch only a configured public video page and extract an allowlisted HTTPS manifest."""
    url=source_page_url(page_path,source_base_url)
    source_host=(urlsplit(source_base_url).hostname or "").lower()
    hosts=host_allowlist(source_base_url,allowed_hosts)
    try:
        response=requests.get(url,headers={"User-Agent":"Mozilla/5.0 (compatible; VideoCatalogue/1.0)",
          "Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.8"},
          timeout=(4,12),allow_redirects=True,stream=True)
    except requests.RequestException as exc:raise StreamResolutionError("Source request failed.") from exc
    try:
        final=urlsplit(response.url)
        if final.scheme!="https" or (final.hostname or "").lower()!=source_host:
            raise StreamResolutionError("Source page redirected to an unapproved host.")
        if response.status_code in (401,403,404):raise StreamResolutionError("Source page is not publicly accessible.")
        response.raise_for_status()
        data=bytearray()
        for chunk in response.iter_content(65536):
            if not chunk:continue
            remaining=MAX_BYTES-len(data)
            if remaining<=0:break
            data.extend(chunk[:remaining])
            if len(chunk)>remaining:break
        page_html=bytes(data).decode(response.encoding or "utf-8",errors="replace")
    except requests.RequestException as exc:raise StreamResolutionError("Could not read source page.") from exc
    finally:response.close()
    return extract_m3u8_url(page_html,url,hosts)
