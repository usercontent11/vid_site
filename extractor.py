#!/usr/bin/env python3
"""Resumable category-card metadata extraction into Neon/PostgreSQL."""
import http.cookies, json, logging, os, re, sys, threading, time, unicodedata, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
import psycopg, requests
from psycopg.rows import dict_row
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://mobilexh.live").strip().rstrip("/")
SOURCE_COOKIE = os.getenv("SOURCE_COOKIE", "")
MAX_PAGES = int(os.getenv("MAX_PAGES", "40"))
WORKERS = int(os.getenv("WORKERS", "4"))
RETRY_FAILED = os.getenv("RETRY_FAILED", "false").lower() in {"1", "true", "yes"}
PAGE_DELAY = max(0.0, float(os.getenv("PAGE_DELAY_SECONDS", "0.25")))
TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
LEASE_MINUTES = max(5, int(os.getenv("LEASE_MINUTES", "15")))
MAX_ATTEMPTS = max(1, int(os.getenv("MAX_CATEGORY_ATTEMPTS", "5")))
SCHEMA_LOCK_KEY = 735119408621
LOG = logging.getLogger("category-extractor")
THREAD = threading.local()

# Preference/age-control cookies supplied for mobilexh.live. The token-like g_state
# value is intentionally not embedded in public source; SOURCE_COOKIE can override
# this list from GitHub Actions secrets if a fresh export is needed.
DEFAULT_COOKIE_FILE = """# Netscape HTTP Cookie File
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tcookie_accept_v2\t%7B%22e%22%3A1%2C%22f%22%3A1%2C%22t%22%3A1%2C%22a%22%3A1%7D
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tx_content_preference_index\tstraight
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tparental-control\tyes
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tstats_src_last\tmobilexh.live
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tx_csrf_token\t1
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tx_viewes\t%5B9981710%2C9996409%5D
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\th_v4_straight\t%7B%22v%22%3A%5B%5D%2C%22l%22%3A%5B%5D%2C%22f%22%3A%5B%5D%2C%22pv%22%3A%5B9996409%2C9981710%5D%7D
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tff_thumb_offset\t6
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tmoments_listing_ad_offset\t6
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tsettings\teyJpc1dlYnBTdXBwb3J0ZWQiOnRydWUsImlzV2VibVN1cHBvcnRlZCI6dHJ1ZSwiZXh0RGV0ZWN0ZWRWMiI6ZmFsc2UsIm1vbWVudHNJc0hpZGRlbiI6bnVsbCwiaXNTaWRlYmFySGlkZGVuIjpudWxsLCJleHBpcmVzIjp7ImV4dERldGVjdGVkVjIiOjE3OTIyNTAyMTB9LCJ0c1Nwb3RDb3VudGVycyI6W3sic3BvdCI6Im1hc3Rlcl9jdWJlIiwidGltZSI6MTc5MjI5MTEyNCwiY291bnQiOjN9LHsic3BvdCI6Im1hc3Rlcl9mb290ZXIiLCJ0aW1lIjoxNzkyMjkxMTI0LCJjb3VudCI6NX0seyJzcG90IjoibmF0aXZlX3RodW1iIiwidGltZSI6MTc5MjI5MTM4NSwiY291bnQiOjF9XX0%3D
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\trecs_show_time\t1792294321
.mobilexh.live\tTRUE\t/\tFALSE\t1823830354\tu-v-channels\tstraight%3A424516-2x273-1x43949-1x425459-1
"""

class SourceFormatError(RuntimeError): pass
class LeaseLostError(RuntimeError): pass

def db():
    if not DATABASE_URL: raise RuntimeError("DATABASE_URL is missing.")
    return psycopg.connect(DATABASE_URL, sslmode=os.getenv("PGSSLMODE", "require"),
                           connect_timeout=20, row_factory=dict_row)

def ensure_schema():
    sql = re.sub(r"(?m)^\s*--.*?$", "", (ROOT / "schema.sql").read_text(encoding="utf-8"))
    with db() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
        for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
            conn.execute(stmt)

def slugify(name):
    value = unicodedata.normalize("NFKD", name.strip().lower())
    out, dash = [], False
    for ch in value.replace("&", " ").replace("’", "").replace("'", ""):
        if unicodedata.combining(ch): continue
        if ch.isalnum(): out.append(ch); dash = False
        elif out and not dash: out.append("-"); dash = True
    return "".join(out).strip("-")

def parse_category_catalog(payload):
    """Use canonical hrefs from grouped category metadata and merge duplicate paths."""
    if isinstance(payload, dict) and isinstance(payload.get("groups"), list):
        grouped_items = []
        for group_obj in payload["groups"]:
            if not isinstance(group_obj, dict):
                raise RuntimeError("Each category group must be an object.")
            group_name = str(group_obj.get("group") or "uncategorized").strip()
            items = group_obj.get("categories")
            if not isinstance(items, list):
                raise RuntimeError(f"Category group {group_name!r} is missing a categories list.")
            for item in items:
                if not isinstance(item, dict):
                    raise RuntimeError(f"Invalid category in group {group_name!r}: expected an object.")
                name = str(item.get("category") or "").strip()
                href = str(item.get("href") or "").strip()
                if not name or not href:
                    raise RuntimeError(f"Category item in group {group_name!r} requires category and href.")
                parsed = urlsplit(href)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise RuntimeError(f"Category href for {name!r} must be an absolute HTTP(S) URL.")
                configured_host = (urlsplit(SITE_BASE_URL).hostname or "").lower()
                href_host = (parsed.hostname or "").lower()
                if configured_host and href_host != configured_host:
                    raise RuntimeError(
                        f"Category href for {name!r} uses host {href_host!r}, "
                        f"but SITE_BASE_URL is configured for {configured_host!r}."
                    )
                path = parsed.path.rstrip("/")
                if not path.startswith("/categories/") or path == "/categories":
                    raise RuntimeError(f"Category href for {name!r} has invalid path {path!r}.")
                grouped_items.append((name, path, group_name))
    else:
        names = payload.get("categories") if isinstance(payload, dict) else payload
        if not isinstance(names, list):
            raise RuntimeError("category_list.json must contain groups[].categories[] entries.")
        grouped_items = []
        for raw in names:
            name = str(raw).strip()
            if name:
                grouped_items.append((name, "/categories/" + slugify(name), "uncategorized"))

    by_path = {}
    seen_slugs = {}
    for name, path, group_name in grouped_items:
        leaf = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        key = slugify(leaf)
        if not key:
            raise RuntimeError(f"Could not create a database key from category path {path!r}.")
        prior_path = seen_slugs.get(key)
        if prior_path is not None and prior_path != path:
            raise RuntimeError(f"Category key collision: {prior_path!r} and {path!r} map to {key!r}.")
        seen_slugs[key] = path
        if path not in by_path:
            by_path[path] = {
                "category_slug": key,
                "category_name": name,
                "category_path": path,
                "category_groups": [],
            }
        elif by_path[path]["category_name"] != name:
            LOG.warning("Path %s appears with labels %r and %r; preserving the first label.",
                        path, by_path[path]["category_name"], name)
        if group_name and group_name not in by_path[path]["category_groups"]:
            by_path[path]["category_groups"].append(group_name)

    categories = list(by_path.values())
    if not categories:
        raise RuntimeError("No categories were found in category_list.json.")
    return categories


def load_categories():
    payload = json.loads((ROOT / "category_list.json").read_text(encoding="utf-8"))
    categories = parse_category_catalog(payload)
    if isinstance(payload, dict) and isinstance(payload.get("groups"), list):
        source_count = sum(len(g.get("categories", [])) for g in payload["groups"] if isinstance(g, dict))
        declared = payload.get("total_categories")
        if declared is not None and int(declared) != source_count:
            LOG.warning("total_categories=%s but found %d grouped entries.", declared, source_count)
        LOG.info("Loaded %d unique categories from %d grouped entries.", len(categories), source_count)
    else:
        LOG.info("Loaded %d categories.", len(categories))
    return categories

def sync_categories(categories):
    sql = """
    INSERT INTO extraction_categories(
      category_slug,category_name,category_path,category_groups,max_pages,status
    ) VALUES (%s,%s,%s,%s,%s,'pending')
    ON CONFLICT(category_slug) DO UPDATE SET
      category_name=EXCLUDED.category_name,
      category_path=EXCLUDED.category_path,
      category_groups=EXCLUDED.category_groups,
      max_pages=GREATEST(extraction_categories.max_pages,EXCLUDED.max_pages),
      status=CASE WHEN extraction_categories.status='completed'
        AND extraction_categories.completion_reason='limit'
        AND GREATEST(extraction_categories.max_pages,EXCLUDED.max_pages)>extraction_categories.last_page
        THEN 'pending' ELSE extraction_categories.status END,
      completion_reason=CASE WHEN extraction_categories.status='completed'
        AND extraction_categories.completion_reason='limit'
        AND GREATEST(extraction_categories.max_pages,EXCLUDED.max_pages)>extraction_categories.last_page
        THEN NULL ELSE extraction_categories.completion_reason END,
      updated_at=now()
    """
    with db() as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(sql, [
            (c["category_slug"],c["category_name"],c["category_path"],c["category_groups"],MAX_PAGES)
            for c in categories
        ])
        if RETRY_FAILED:
            conn.execute("""UPDATE extraction_categories SET status='pending',attempts=0,consecutive_failures=0,
              last_error=NULL,lease_owner=NULL,lease_expires_at=NULL,completion_reason=NULL,updated_at=now()
              WHERE status='failed'""")
    LOG.info("Registered %d unique categories. Max page cap is %d.", len(categories), MAX_PAGES)

def claim(owner):
    sql = """
    WITH chosen AS (
      SELECT category_slug FROM extraction_categories WHERE next_page<=max_pages AND
        (status='pending' OR (status='processing' AND (lease_expires_at IS NULL OR lease_expires_at<now())))
      ORDER BY category_slug FOR UPDATE SKIP LOCKED LIMIT 1
    )
    UPDATE extraction_categories q SET status='processing',lease_owner=%s,
      lease_expires_at=now()+(%s*interval '1 minute'),attempts=attempts+1,updated_at=now()
    FROM chosen WHERE q.category_slug=chosen.category_slug
    RETURNING q.category_slug,q.category_name,q.category_path,q.next_page,q.max_pages
    """
    with db() as conn, conn.transaction(): return conn.execute(sql,(owner,LEASE_MINUTES)).fetchone()

def apply_cookie_secret(session):
    raw = SOURCE_COOKIE.strip() or DEFAULT_COOKIE_FILE
    if "\\n" in raw and "\n" not in raw: raw = raw.replace("\\r\\n","\n").replace("\\n","\n")
    escaped = raw.lstrip().startswith("\\# Netscape")
    count = 0
    for original in raw.splitlines():
        line = original.strip()
        if escaped:
            if line.startswith("\\#"): line=line[1:]
            if line.endswith("\\"): line=line[:-1].rstrip()
        if not line or line.startswith("#") or line=="\\": continue
        f = line.split("\t")
        if len(f)>=7:
            domain,_sub,path,secure,expires,name,value=f[:7]
            try: exp=int(expires)
            except ValueError: exp=0
            if exp and exp<=int(time.time()): continue
            session.cookies.set(name,value,domain=domain,path=path or "/",secure=secure.upper()=="TRUE")
            count+=1
            continue
        parsed=http.cookies.SimpleCookie()
        try: parsed.load(line[7:].strip() if line.lower().startswith("cookie:") else line)
        except http.cookies.CookieError: continue
        for name,morsel in parsed.items():
            session.cookies.set(name,morsel.value,domain=urlsplit(SITE_BASE_URL).hostname,path="/")
            count+=1
    LOG.info("Loaded %d request cookies; cookie values are not logged.",count)

def http_session():
    sess=getattr(THREAD,"session",None)
    if sess is None:
        retry=Retry(total=3,connect=3,read=3,status=3,backoff_factor=0.8,
                    status_forcelist=(429,500,502,503,504),allowed_methods=frozenset({"GET"}),
                    respect_retry_after_header=True)
        sess=requests.Session()
        adapter=HTTPAdapter(max_retries=retry,pool_connections=2,pool_maxsize=2)
        sess.mount("https://",adapter); sess.mount("http://",adapter)
        sess.headers.update({"User-Agent":"Mozilla/5.0 (compatible; CategoryMetadataIndexer/1.0)",
          "Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.8"})
        apply_cookie_secret(sess); THREAD.session=sess
    return sess

def make_page_path(category_path,page):
    base=category_path.rstrip("/")
    return base if page==1 else f"{base}/{page}"

def absolute_url(path): return urljoin(SITE_BASE_URL+"/",quote(path,safe="/%:@"))

def parse_initials(html):
    m=re.search(r'<script\b[^>]*\bid=["\']initials-script["\'][^>]*>(.*?)</script\s*>',html,re.I|re.S)
    if not m: raise SourceFormatError("initials-script missing; the server may have returned a challenge page.")
    raw=re.sub(r"^\s*window\.initials\s*=\s*","",m.group(1).strip(),count=1).strip()
    if raw.endswith(";"): raw=raw[:-1]
    try: value=json.loads(raw)
    except json.JSONDecodeError as exc: raise SourceFormatError(f"Initial data JSON invalid: {exc}") from exc
    if not isinstance(value,dict): raise SourceFormatError("Initial data isn't an object.")
    return value

def validate_category_context(data, category, final_path, channel, cards):
    """Validate a returned page by its category slug and cards, not tagType alone."""
    if not isinstance(channel, dict):
        raise SourceFormatError(
            f"Expected category metadata for {category['category_slug']!r}; channelInfo is "
            f"{type(channel).__name__}, currentPage={data.get('currentPage')!r}, "
            f"final_path={final_path!r}, initial_keys={list(data)[:12]!r}. "
            "The server may have returned an age-check, anti-bot, or fallback page."
        )
    if not isinstance(cards, list):
        raise SourceFormatError(
            f"Expected videoThumbProps list for {category['category_slug']!r}; found {type(cards).__name__}; "
            f"tagType={channel.get('tagType')!r}, slug={channel.get('slug')!r}, "
            f"inurl={channel.get('inurl')!r}, currentPage={data.get('currentPage')!r}."
        )
    expected_slug = slugify(unquote(category["category_path"].rstrip("/").rsplit("/", 1)[-1]))
    actual_raw = channel.get("slug") or channel.get("inurl")
    actual_slug = slugify(unquote(str(actual_raw).strip("/").rsplit("/", 1)[-1])) if actual_raw else ""
    tag_type = str(channel.get("tagType") or "").strip().lower()
    if actual_slug and actual_slug != expected_slug:
        raise SourceFormatError(
            f"Requested category slug={expected_slug!r}, but source page identifies "
            f"{actual_slug!r}; tagType={tag_type!r}, final_path={final_path!r}, "
            f"currentPage={data.get('currentPage')!r}. Check the category href in category_list.json."
        )
    if tag_type != "category" and actual_slug != expected_slug:
        raise SourceFormatError(
            f"Unverified category response for {expected_slug!r}: tagType={tag_type!r}, "
            f"page_slug={actual_slug!r}, final_path={final_path!r}, currentPage={data.get('currentPage')!r}."
        )
    if tag_type != "category":
        LOG.warning("Accepting %s: matching slug and cards, internal tagType=%r.",
                    category["category_name"], tag_type)


def fetch_page(category,page):
    path=make_page_path(category["category_path"],page)
    response=http_session().get(absolute_url(path),timeout=TIMEOUT)
    if response.status_code in (404,410) and page>1: return None
    response.raise_for_status()
    final=urlsplit(response.url)
    if final.netloc.lower()!=urlsplit(SITE_BASE_URL).netloc.lower():
        raise SourceFormatError(f"Request redirected to another host ({final.netloc!r}).")
    if not final.path.startswith("/categories/"):
        raise SourceFormatError(f"Requested {path!r}, but response redirected to {final.path!r}.")
    data=parse_initials(response.text)
    channel=data.get("channelInfo")
    cards=((data.get("pagesCategoryComponent") or {}).get("trendingVideoListProps") or {}).get("videoThumbProps")
    validate_category_context(data,category,final.path,channel,cards)
    reported=data.get("activePage",data.get("page"))
    if reported is not None:
        try: reported=int(reported)
        except (TypeError,ValueError) as exc:
            raise SourceFormatError(f"Invalid page indicator {reported!r} for page {page}.") from exc
        if reported!=page: raise SourceFormatError(f"Requested page {page}; source rendered page {reported}.")
    return cards

def path_only(value):
    return urlsplit(value.strip()).path or None if isinstance(value,str) and value.strip() else None

def cdn_version(*values):
    for value in values:
        if isinstance(value,str):
            m=re.search(r"(?:^|\.)thumb-v(\d+)\.cdnsolutions\.media$", (urlsplit(value).hostname or "").lower())
            if m: return int(m.group(1))
    return None

def detect_access_status(card):
    markers={"ispremium","ispremiumvideo","premiumonly","haspremiumbadge","ispaid","islocked",
             "ispaywalled","paywalled","accesslevel","accessstatus","videoaccesslevel","isrestricted"}
    premium=public=False
    for key,value in card.items():
        norm=re.sub(r"[^a-z0-9]","",str(key).lower())
        if norm.endswith("enabled") or norm=="haspremiumsale": continue
        if norm not in markers and "premium" not in norm: continue
        text=value.strip().lower() if isinstance(value,str) else ""
        if value is True or text in {"premium","paid","locked","restricted","paywalled"}: premium=True
        elif value is False and norm not in {"accesslevel","accessstatus"}: public=True
        elif text in {"free","public","non-premium","nonpremium"}: public=True
    return "premium" if premium else ("non_premium" if public else "unknown")

def normalize_card(card):
    if not isinstance(card,dict) or card.get("id") is None: return None
    if not isinstance(card.get("pageURL"),str) or not isinstance(card.get("title"),str): return None
    kind=str(card.get("videoType") or "video").strip().lower()
    if kind and kind!="video": return None
    access=detect_access_status(card)
    if access=="premium": return None
    try:
        vid=int(card["id"])
        duration=int(card["duration"]) if card.get("duration") is not None else None
        created=int(card["created"]) if card.get("created") is not None else None
        views=int(card["views"]) if card.get("views") is not None else None
        thumb_id=int(card["thumbId"]) if card.get("thumbId") is not None else None
    except (TypeError,ValueError): return None
    sprite,trailer,fallback=card.get("spriteURL"),card.get("trailerURL"),card.get("trailerFallbackUrl")
    return {
      "id":vid,"title":card["title"].strip(),"page_path":path_only(card.get("pageURL")),
      "duration_seconds":duration,"created_epoch":created,"views":views,"video_type":kind or "video",
      "thumb_id":thumb_id,"thumbnail_path":path_only(card.get("thumbURL")),
      "image_path":path_only(card.get("imageURL")),"preview_thumb_path":path_only(card.get("previewThumbURL")),
      "sprite_path":path_only(sprite),"trailer_path":path_only(trailer),
      "trailer_fallback_path":path_only(fallback),"trailer_cdn_version":cdn_version(sprite,trailer,fallback),
      "access_status":access
    }

UPSERT_VIDEO = """
INSERT INTO videos(id,title,page_path,duration_seconds,created_epoch,views,video_type,thumb_id,
 thumbnail_path,image_path,preview_thumb_path,sprite_path,trailer_path,trailer_fallback_path,trailer_cdn_version,access_status)
VALUES (%(id)s,%(title)s,%(page_path)s,%(duration_seconds)s,%(created_epoch)s,%(views)s,%(video_type)s,%(thumb_id)s,
 %(thumbnail_path)s,%(image_path)s,%(preview_thumb_path)s,%(sprite_path)s,%(trailer_path)s,%(trailer_fallback_path)s,
 %(trailer_cdn_version)s,%(access_status)s)
ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title,page_path=EXCLUDED.page_path,
 duration_seconds=EXCLUDED.duration_seconds,created_epoch=EXCLUDED.created_epoch,views=EXCLUDED.views,
 video_type=EXCLUDED.video_type,thumb_id=EXCLUDED.thumb_id,thumbnail_path=EXCLUDED.thumbnail_path,
 image_path=EXCLUDED.image_path,preview_thumb_path=EXCLUDED.preview_thumb_path,sprite_path=EXCLUDED.sprite_path,
 trailer_path=EXCLUDED.trailer_path,trailer_fallback_path=EXCLUDED.trailer_fallback_path,
 trailer_cdn_version=EXCLUDED.trailer_cdn_version,
 access_status=CASE WHEN EXCLUDED.access_status<>'unknown' THEN EXCLUDED.access_status ELSE videos.access_status END,
 last_seen_at=now()
"""

def save_page(category,owner,page,rows):
    with db() as conn, conn.transaction(), conn.cursor() as cur:
        if rows:
            cur.executemany(UPSERT_VIDEO,rows)
            cur.executemany("""INSERT INTO video_categories(category_slug,video_id) VALUES (%s,%s)
              ON CONFLICT(category_slug,video_id) DO UPDATE SET last_seen_at=now()""",
              [(category["category_slug"],r["id"]) for r in rows])
        saved=conn.execute("""
          UPDATE extraction_categories
          SET last_page=%s,next_page=%s,status=CASE WHEN %s>=max_pages THEN 'completed' ELSE 'processing' END,
            completion_reason=CASE WHEN %s>=max_pages THEN 'limit' ELSE NULL END,
            lease_owner=CASE WHEN %s>=max_pages THEN NULL ELSE lease_owner END,
            lease_expires_at=CASE WHEN %s>=max_pages THEN NULL ELSE now()+(%s*interval '1 minute') END,
            consecutive_failures=0,last_error=NULL,updated_at=now()
          WHERE category_slug=%s AND status='processing' AND lease_owner=%s
          RETURNING status,max_pages,next_page
        """,(page,page+1,page,page,page,page,LEASE_MINUTES,category["category_slug"],owner)).fetchone()
        if not saved: raise LeaseLostError(f"Lease lost for {category['category_name']}; page transaction rolled back.")
        return saved

def mark_end(category,owner,page):
    with db() as conn, conn.transaction():
        row=conn.execute("""
          UPDATE extraction_categories SET status='completed',completion_reason='end',next_page=%s,
            last_page=GREATEST(last_page,%s),lease_owner=NULL,lease_expires_at=NULL,
            consecutive_failures=0,last_error=NULL,updated_at=now()
          WHERE category_slug=%s AND status='processing' AND lease_owner=%s RETURNING category_slug
        """,(page,page-1,category["category_slug"],owner)).fetchone()
        if not row: raise LeaseLostError(f"Lease lost while finishing {category['category_name']}.")

def release_error(category,owner,exc):
    try:
        with db() as conn, conn.transaction():
            conn.execute("""
              UPDATE extraction_categories SET consecutive_failures=consecutive_failures+1,
                status=CASE WHEN consecutive_failures+1 >= %s THEN 'failed' ELSE 'pending' END,
                lease_owner=NULL,lease_expires_at=NULL,last_error=%s,updated_at=now()
              WHERE category_slug=%s AND status='processing' AND lease_owner=%s
            """,(MAX_ATTEMPTS,f"{type(exc).__name__}: {exc}"[:3000],category["category_slug"],owner))
    except Exception: LOG.exception("Could not release failed category %s.",category["category_name"])

def process_category(category,owner):
    out={"category":category["category_name"],"pages":0,"videos":0,"status":"unknown"}
    page=int(category["next_page"])
    try:
        while page<=int(category["max_pages"]):
            cards=fetch_page(category,page)
            if cards is None or not cards:
                if page==1: raise SourceFormatError("First page is empty; refusing to mark category complete.")
                mark_end(category,owner,page); out["status"]="completed_end"; return out
            rows,seen=[],set()
            for card in cards:
                record=normalize_card(card)
                if record and record["id"] not in seen:
                    rows.append(record); seen.add(record["id"])
            state=save_page(category,owner,page,rows)
            out["pages"]+=1; out["videos"]+=len(rows)
            page=int(state["next_page"]); category["max_pages"]=int(state["max_pages"])
            if state["status"]=="completed": out["status"]="completed_limit"; return out
            if PAGE_DELAY: time.sleep(PAGE_DELAY)
        out["status"]="completed_limit"
    except LeaseLostError:
        out["status"]="lease_lost"; LOG.warning("Lease reclaimed for %s.",category["category_name"])
    except Exception as exc:
        out["status"]="error"; LOG.exception("%s failed on page %d.",category["category_name"],page)
        release_error(category,owner,exc)
    return out

def worker_loop(run_id):
    results=[]
    while True:
        owner=f"{run_id}:{uuid.uuid4().hex[:12]}"
        category=claim(owner)
        if category is None: return results
        LOG.info("Claimed %s starting at page %s/%s.",category["category_name"],category["next_page"],category["max_pages"])
        results.append(process_category(category,owner))

def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO").upper(),
      format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    if not DATABASE_URL: LOG.error("Set DATABASE_URL in Actions secrets."); return 2
    if not SITE_BASE_URL: LOG.error("Set SITE_BASE_URL in Actions variables or secrets."); return 2
    if not 1<=MAX_PAGES<=2000: LOG.error("MAX_PAGES must be 1..2000."); return 2
    if not 1<=WORKERS<=16: LOG.error("WORKERS must be 1..16."); return 2
    try:
        categories=load_categories(); ensure_schema(); sync_categories(categories)
    except Exception: LOG.exception("Initialization failed."); return 1
    run_id=f"{os.getenv('GITHUB_RUN_ID','local')}:{os.getenv('GITHUB_RUN_ATTEMPT','1')}:{uuid.uuid4().hex[:8]}"
    results=[]; crashed=0
    LOG.info("Start: %d categories, cap=%d, workers=%d.",len(categories),MAX_PAGES,WORKERS)
    with ThreadPoolExecutor(max_workers=WORKERS,thread_name_prefix="category") as pool:
        futures=[pool.submit(worker_loop,run_id) for _ in range(WORKERS)]
        for future in as_completed(futures):
            try: results.extend(future.result())
            except Exception: crashed+=1; LOG.exception("Worker crashed.")
    LOG.info("This run: %d category claims, %d pages, %d video rows.",
      len(results),sum(x["pages"] for x in results),sum(x["videos"] for x in results))
    try:
        with db() as conn:
            counts=conn.execute("""SELECT
              count(*) FILTER (WHERE status='pending') pending,
              count(*) FILTER (WHERE status='processing') processing,
              count(*) FILTER (WHERE status='completed') completed,
              count(*) FILTER (WHERE status='failed') failed,
              (SELECT count(*) FROM videos) videos,
              (SELECT count(*) FROM video_categories) category_video_links
              FROM extraction_categories""").fetchone()
            print(json.dumps(counts,indent=2,default=str))
    except Exception: crashed+=1; LOG.exception("Could not query final counts.")
    return 1 if crashed or any(x["status"]=="error" for x in results) else 0

if __name__=="__main__":
    sys.exit(main())
