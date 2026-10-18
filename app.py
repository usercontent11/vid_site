"""Server-rendered Flask video site for Vercel and Neon PostgreSQL."""
from __future__ import annotations
import html, hmac, json, logging, math, os, re
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET
import psycopg
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from psycopg.rows import dict_row
from resolver import StreamResolutionError, inspect_source_page_structure, resolve_stream_source
from extractor import http_session

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("video-site")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SOURCE_BASE_URL = os.getenv("SOURCE_BASE_URL", "https://mobilexh.live").strip().rstrip("/")
ASSET_BASE_URL = os.getenv("ASSET_BASE_URL", "https://ic-vt-nss.cdnsolutions.media").strip().rstrip("/")
SITE_NAME = os.getenv("SITE_NAME", "Video Library").strip() or "Video Library"
try: PER_PAGE = min(60, max(1, int(os.getenv("PER_PAGE", "30"))))
except ValueError: PER_PAGE = 30
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
ET.register_namespace("", NS)
SITEMAP_CHUNK = 20000
VISIBLE = "access_status NOT IN ('premium', 'inaccessible')"

def db():
    if not DATABASE_URL: raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg.connect(DATABASE_URL, sslmode=os.getenv("PGSSLMODE", "require"),
                           connect_timeout=10, row_factory=dict_row)

def base_url():
    configured = os.getenv("SITE_URL", "").strip().rstrip("/")
    if configured: return configured
    host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "").strip() or os.getenv("VERCEL_URL", "").strip()
    return "https://" + host.removeprefix("https://").removeprefix("http://").rstrip("/") if host else request.url_root.rstrip("/")

def abs_url(path): return base_url() + (path if path.startswith("/") else "/" + path)

def internal_path(value):
    if not isinstance(value, str) or not value.strip(): return None
    p = urlsplit(value.strip())
    if p.scheme or p.netloc or p.query or p.fragment or not p.path.startswith("/") or p.path.startswith("//"): return None
    if not (p.path.startswith("/videos/") or p.path.startswith("/categories/")): return None
    return quote(p.path, safe="/%:@!$&'()*+,;=-._~")

def asset_url(value):
    if not isinstance(value, str) or not value.strip(): return None
    p = urlsplit(value.strip())
    if p.scheme: return value.strip() if p.scheme in ("https", "http") and p.netloc else None
    if p.netloc or value.startswith("//"): return None
    return urljoin(ASSET_BASE_URL + "/", value.strip().lstrip("/"))

def clean_text(value):
    if not isinstance(value, str): return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value))).strip()

def duration(seconds):
    try: n = int(seconds)
    except (TypeError, ValueError): return ""
    if n <= 0: return ""
    h, rem = divmod(n, 3600); m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def views_label(value):
    try: n = int(value)
    except (TypeError, ValueError): return "Views unavailable"
    if n < 0: return "Views unavailable"
    if n >= 1_000_000: return f"{n / 1_000_000:.1f}M views"
    if n >= 1000: return f"{n / 1000:.1f}K views"
    return f"{n:,} views"

app.jinja_env.filters["duration"] = duration
app.jinja_env.filters["views"] = views_label

@app.context_processor
def globals_for_templates():
    return {"site_name": SITE_NAME, "site_base": base_url(), "year": datetime.now(timezone.utc).year}

@app.after_request
def headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.path.startswith("/api/stream/") or request.path.startswith("/api/diagnostics/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response

def decorate_video(row):
    v = dict(row)
    v["thumbnail_url"] = asset_url(v.get("thumbnail_path")) or asset_url(v.get("image_path")) or asset_url(v.get("preview_thumb_path"))
    safe_href = internal_path(v.get("page_path"))
    v["href"] = safe_href.rstrip("/") + "/" if safe_href else None
    v["duration_label"] = duration(v.get("duration_seconds"))
    return v

def cats(limit=12):
    with db() as conn:
        rows = conn.execute(f"""
          SELECT c.category_slug,c.category_name,c.category_path,
                 COUNT(v.id) FILTER (WHERE {VISIBLE})::bigint AS video_count
          FROM extraction_categories c
          LEFT JOIN video_categories vc ON vc.category_slug=c.category_slug
          LEFT JOIN videos v ON v.id=vc.video_id
          WHERE c.enabled=TRUE
          GROUP BY c.category_slug,c.category_name,c.category_path
          ORDER BY video_count DESC,lower(c.category_name),c.category_slug LIMIT %s
        """, (limit,)).fetchall()
    out=[]
    for row in rows:
        item=dict(row); href=internal_path(item.get("category_path")); item["href"]=href.rstrip("/")+"/" if href else None
        out.append(item)
    return out

def fetch_listing(page=1, category_slug=None, query=None):
    offset=(page-1)*PER_PAGE
    with db() as conn:
        if category_slug:
            total=conn.execute(f"SELECT COUNT(*)::bigint n FROM video_categories vc JOIN videos v ON v.id=vc.video_id WHERE vc.category_slug=%s AND v.{VISIBLE}",(category_slug,)).fetchone()["n"]
            rows=conn.execute(f"""SELECT v.* FROM video_categories vc JOIN videos v ON v.id=vc.video_id
              WHERE vc.category_slug=%s AND v.{VISIBLE} ORDER BY v.created_epoch DESC NULLS LAST,v.views DESC NULLS LAST,v.id DESC LIMIT %s OFFSET %s""",(category_slug,PER_PAGE,offset)).fetchall()
        elif query is not None:
            pattern="%"+query+"%"
            total=conn.execute(f"SELECT COUNT(*)::bigint n FROM videos WHERE {VISIBLE} AND (title ILIKE %s OR COALESCE(description,'') ILIKE %s)",(pattern,pattern)).fetchone()["n"]
            rows=conn.execute(f"""SELECT * FROM videos WHERE {VISIBLE} AND (title ILIKE %s OR COALESCE(description,'') ILIKE %s)
              ORDER BY views DESC NULLS LAST,created_epoch DESC NULLS LAST,id DESC LIMIT %s OFFSET %s""",(pattern,pattern,PER_PAGE,offset)).fetchall()
        else:
            total=conn.execute(f"SELECT COUNT(*)::bigint n FROM videos WHERE {VISIBLE}").fetchone()["n"]
            rows=conn.execute(f"""SELECT * FROM videos WHERE {VISIBLE}
              ORDER BY created_epoch DESC NULLS LAST,views DESC NULLS LAST,id DESC LIMIT %s OFFSET %s""",(PER_PAGE,offset)).fetchall()
    return int(total), [decorate_video(r) for r in rows]

def pages(total): return max(1, math.ceil(max(0,total)/PER_PAGE))

def pagination(page,total,builder):
    last=pages(total)
    start=max(1,page-2); end=min(last,start+4); start=max(1,end-4)
    return {"page":page,"total_pages":last,"total":total,
            "items":[{"number":x,"url":builder(x),"current":x==page} for x in range(start,end+1)],
            "previous":builder(page-1) if page>1 else None,
            "next":builder(page+1) if page<last else None}

def valid_page(page,total):
    if page < 1 or page > pages(total): abort(404)

def home_url(p): return "/" if p==1 else url_for("home_page",page=p)

def render_home(page):
    total,videos=fetch_listing(page)
    valid_page(page,total)
    return render_template("listing.html", title=("Page "+str(page)+" – " if page>1 else "")+"Discover Videos",
      meta_description="Browse the latest videos, explore categories, and discover related videos in a searchable video library.",
      canonical_url=abs_url(home_url(page)), robots_meta="index,follow", mode="home", heading="Discover videos",
      intro="Browse the latest additions, explore categories, and find related videos from one place.",
      videos=videos,categories=cats(12),pagination=pagination(page,total,home_url),page=page,total=total)

@app.get("/")
def home(): return render_home(1)

@app.get("/page/<int:page>/")
def home_page(page):
    if page==1: return redirect(url_for("home"),code=301)
    return render_home(page)

@app.get("/categories/")
def categories_index():
    return render_template("categories.html",title="Video Categories",
      meta_description="Browse video categories and explore related videos.",
      canonical_url=abs_url("/categories/"),robots_meta="index,follow",categories=cats(1000))

@app.get("/categories/<path:category_path>")
def category_page(category_path):
    request_path="/"+request.path.strip("/")
    parts=request_path.split("/")
    page=1
    if len(parts)>2 and parts[-1].isdigit():
        page=int(parts[-1]); lookup="/"+"/".join(parts[1:-1])
    else: lookup=request_path
    with db() as conn:
        category=conn.execute("""SELECT category_slug,category_name,category_path FROM extraction_categories
          WHERE enabled=TRUE AND rtrim(category_path,'/')=rtrim(%s,'/') LIMIT 1""",(lookup,)).fetchone()
        if not category:
            leaf=lookup.rstrip("/").rsplit("/",1)[-1]
            category=conn.execute("SELECT category_slug,category_name,category_path FROM extraction_categories WHERE enabled=TRUE AND category_slug=%s LIMIT 1",(leaf,)).fetchone()
    if not category: abort(404)
    category=dict(category); cat_path=internal_path(category.get("category_path"))
    if not cat_path or not cat_path.startswith("/categories/"): abort(404)
    cat_path=cat_path.rstrip("/")+"/"
    total,videos=fetch_listing(page,category_slug=category["category_slug"]); valid_page(page,total)
    def cat_page_url(p): return cat_path if p==1 else cat_path.rstrip("/")+f"/{p}/"
    current=cat_page_url(page)
    return render_template("listing.html",title=f"{category['category_name']} Videos"+(f" – Page {page}" if page>1 else ""),
      meta_description=f"Browse {category['category_name']} videos and explore related clips.",
      canonical_url=abs_url(current),robots_meta="index,follow",mode="category",
      heading=f"{category['category_name']} videos",intro=f"Browse videos in {category['category_name']} and discover related clips.",
      videos=videos,categories=[],category=category,pagination=pagination(page,total,cat_page_url),page=page,total=total)

@app.get("/search")
def search():
    q=re.sub(r"\s+"," ",request.args.get("q","").strip())[:120]
    try: page=max(1,int(request.args.get("page","1")))
    except ValueError: page=1
    total,videos=fetch_listing(page,query=q) if q else (0,[])
    if q: valid_page(page,total)
    return render_template("listing.html",title=("Search results for "+q) if q else "Search Videos",
      meta_description=("Search results for "+q) if q else "Search video titles and descriptions.",
      canonical_url=abs_url("/search"),robots_meta="noindex,follow",mode="search",
      heading=("Results for “"+q+"”") if q else "Search videos",
      intro=(f"{total} matching videos" if q else "Search video titles and available descriptions."),
      videos=videos,categories=[],query=q,
      pagination=pagination(page,total,lambda p:url_for("search",q=q,page=p)) if q else None,page=page,total=total)

def video_categories(video_id):
    with db() as conn:
        rows=conn.execute("""SELECT c.category_slug,c.category_name,c.category_path
          FROM extraction_categories c JOIN video_categories vc ON vc.category_slug=c.category_slug
          WHERE vc.video_id=%s AND c.enabled=TRUE ORDER BY lower(c.category_name) LIMIT 8""",(video_id,)).fetchall()
    result=[]
    for row in rows:
        x=dict(row);x["href"]=internal_path(x.get("category_path"))
        result.append(x)
    return result

def related(video_id, limit=12):
    with db() as conn:
        rows=conn.execute(f"""SELECT v.*,COUNT(DISTINCT rel.category_slug)::int shared
          FROM videos v LEFT JOIN video_categories rel ON rel.video_id=v.id
           AND rel.category_slug IN (SELECT category_slug FROM video_categories WHERE video_id=%s)
          WHERE v.id<>%s AND v.{VISIBLE} GROUP BY v.id
          HAVING COUNT(DISTINCT rel.category_slug)>0
          ORDER BY shared DESC,v.views DESC NULLS LAST,v.id DESC LIMIT %s""",(video_id,video_id,limit)).fetchall()
        out=[dict(r) for r in rows]
        if len(out)<limit:
            excluded=[r["id"] for r in out]+[video_id]
            fallback=conn.execute(f"""SELECT * FROM videos WHERE {VISIBLE} AND NOT(id=ANY(%s))
              ORDER BY created_epoch DESC NULLS LAST,views DESC NULLS LAST,id DESC LIMIT %s""",(excluded,limit-len(out))).fetchall()
            out.extend(dict(r) for r in fallback)
    return [decorate_video(r) for r in out]

def upload_iso(value):
    try:
        n=int(value)
        if n>100_000_000_000:n//=1000
        return datetime.fromtimestamp(n,tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError,ValueError,OSError,OverflowError): return None

def duration_iso(value):
    try:n=int(value)
    except (TypeError,ValueError):return None
    return f"PT{n}S" if n>0 else None

@app.get("/videos/<path:video_path>")
def video_page(video_path):
    path="/"+request.path.lstrip("/")
    with db() as conn:
        row=conn.execute("SELECT * FROM videos WHERE rtrim(page_path,'/')=rtrim(%s,'/') LIMIT 1",(path,)).fetchone()
    if not row or row.get("access_status") in {"premium","inaccessible"}: abort(404)
    v=decorate_video(row)
    canonical=internal_path(v.get("page_path"))
    if not canonical: abort(404)
    canonical=canonical.rstrip("/")+"/"
    if request.path.rstrip("/") == canonical.rstrip("/") and not request.path.endswith("/"):
        return redirect(canonical,code=301)
    vcats=video_categories(v["id"])
    text=clean_text(v.get("description"))
    if not text:
        names=[c["category_name"] for c in vcats[:3]]
        suffix=" in "+", ".join(names) if names else ""
        text=f"Watch {v['title']}{suffix}. The original description is not available yet; browse the related videos below to discover more."
    schema=None; date=upload_iso(v.get("created_epoch"))
    if v.get("thumbnail_url") and date:
        schema={"@context":"https://schema.org","@type":"VideoObject","name":v["title"],
          "description":text[:2048],"thumbnailUrl":[v["thumbnail_url"]],"uploadDate":date,
          "url":abs_url(canonical),"embedUrl":abs_url(url_for("embed",video_id=v["id"]))}
        d=duration_iso(v.get("duration_seconds"))
        if d:schema["duration"]=d
        try:n=int(v.get("views") or 0)
        except (ValueError,TypeError):n=0
        if n>0:schema["interactionStatistic"]={"@type":"InteractionCounter","interactionType":{"@type":"WatchAction"},"userInteractionCount":n}
    breadcrumb=[{"@type":"ListItem","position":1,"name":"Home","item":abs_url("/")}]
    if vcats and vcats[0].get("href"):
        breadcrumb.append({"@type":"ListItem","position":2,"name":vcats[0]["category_name"],"item":abs_url(vcats[0]["href"])})
    breadcrumb.append({"@type":"ListItem","position":len(breadcrumb)+1,"name":v["title"],"item":abs_url(canonical)})
    return render_template("video.html",title=v["title"],meta_description=text[:300],
      canonical_url=abs_url(canonical),robots_meta="index,follow",video=v,description=text,
      categories=vcats,related=related(v["id"]),video_schema=schema,
      breadcrumb_schema={"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":breadcrumb},
      uploaded= date[:10] if date else None)

@app.get("/embed/<int:video_id>/")
def embed(video_id):
    with db() as conn:
        row=conn.execute(f"SELECT * FROM videos WHERE id=%s AND {VISIBLE}",(video_id,)).fetchone()
    if not row: abort(404)
    return render_template("embed.html",video=decorate_video(row)),200,{"X-Robots-Tag":"noindex, nofollow"}

@app.get("/api/stream/<int:video_id>")
def stream_api(video_id):
    """Resolve a manifest on demand. No M3U8 URL is persisted or cached."""
    with db() as conn:
        video = conn.execute(
            "SELECT id, page_path, access_status FROM videos WHERE id = %s",
            (video_id,),
        ).fetchone()
    if not video or video.get("access_status") in {"premium", "inaccessible"}:
        return jsonify({"error": "Video is not available for playback."}), 404
    try:
        stream_url, stream_type = resolve_stream_source(video["page_path"], SOURCE_BASE_URL)
    except StreamResolutionError as exc:
        log.warning("Stream resolution failed for video id=%s: %s", video_id, exc)
        return jsonify({"error": "A playable stream was not found for this video."}), 503
    return jsonify({"url": stream_url, "type": stream_type})

@app.get("/sitemap.xml")
def sitemap_index():
    with db() as conn: total=conn.execute(f"SELECT COUNT(*)::bigint n FROM videos WHERE {VISIBLE}").fetchone()["n"]
    root=ET.Element(f"{{{NS}}}sitemapindex")
    for path in ["/sitemaps/static.xml"]+[f"/sitemaps/videos-{i}.xml" for i in range(1,max(1,math.ceil(int(total)/SITEMAP_CHUNK))+1)]:
        node=ET.SubElement(root,f"{{{NS}}}sitemap");xloc(node,"loc",abs_url(path))
    return xmlout(root)

@app.get("/sitemaps/static.xml")
def static_sitemap():
    root=ET.Element(f"{{{NS}}}urlset")
    paths=["/","/categories/"]+[c["href"].rstrip("/")+"/" for c in cats(49000) if c.get("href")]
    for path in dict.fromkeys(paths):
        node=ET.SubElement(root,f"{{{NS}}}url");xloc(node,"loc",abs_url(path))
    return xmlout(root)

@app.get("/sitemaps/videos-<int:chunk>.xml")
def video_sitemap(chunk):
    if chunk<1:abort(404)
    offset=(chunk-1)*SITEMAP_CHUNK
    with db() as conn:
        total=conn.execute(f"SELECT COUNT(*)::bigint n FROM videos WHERE {VISIBLE}").fetchone()["n"]
        if chunk>max(1,math.ceil(int(total)/SITEMAP_CHUNK)):abort(404)
        rows=conn.execute(f"SELECT page_path,last_seen_at FROM videos WHERE {VISIBLE} ORDER BY id DESC LIMIT %s OFFSET %s",(SITEMAP_CHUNK,offset)).fetchall()
    root=ET.Element(f"{{{NS}}}urlset")
    for row in rows:
        path=internal_path(row.get("page_path"))
        if not path or not path.startswith("/videos/"):continue
        node=ET.SubElement(root,f"{{{NS}}}url");xloc(node,"loc",abs_url(path.rstrip("/")+"/"))
        last=row.get("last_seen_at")
        if isinstance(last,datetime):xloc(node,"lastmod",last.astimezone(timezone.utc).date().isoformat())
    return xmlout(root)

@app.get("/robots.txt")
def robots():
    return (f"User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {abs_url('/sitemap.xml')}\n",200,{"Content-Type":"text/plain; charset=utf-8"})

@app.get("/api/diagnostics/source-page/<int:video_id>")
def source_page_diagnostics(video_id):
    """Temporary, token-protected structural diagnostics; never returns raw page HTML."""
    expected = os.getenv("STREAM_DIAGNOSTICS_TOKEN", "").strip()
    supplied = request.headers.get("X-Diagnostics-Token", "").strip()
    if not expected:
        abort(404)
    if not supplied or not hmac.compare_digest(supplied, expected):
        abort(403)

    with db() as conn:
        video = conn.execute(
            "SELECT id, page_path, access_status FROM videos WHERE id = %s",
            (video_id,),
        ).fetchone()
    if not video or video.get("access_status") in {"premium", "inaccessible"}:
        abort(404)

    try:
        report = inspect_source_page_structure(video["page_path"], SOURCE_BASE_URL)
        cookies = http_session().cookies
        report["request_session"] = {
            "cookie_count": len(cookies),
            "cookie_names": sorted({c.name for c in cookies}),
            "g_state_present": any(c.name == "g_state" for c in cookies),
            "cookie_values_returned": False,
        }
        report["video_id"] = int(video_id)
        report["source_path"] = internal_path(video.get("page_path"))
        return jsonify(report)
    except StreamResolutionError as exc:
        return jsonify({"video_id": video_id, "error": str(exc)}), 502
    finally:
        # The page structure and query-free URL paths are sensitive diagnostics.
        pass


@app.get("/healthz")
def healthz():
    if not DATABASE_URL:return jsonify({"ok":False,"reason":"DATABASE_URL missing"}),503
    try:
        with db() as conn:conn.execute("SELECT 1")
        return jsonify({"ok":True})
    except Exception:
        log.exception("Health check failed");return jsonify({"ok":False,"reason":"database unavailable"}),503

@app.errorhandler(404)
def missing(_error):
    return render_template("404.html",title="Page Not Found",meta_description="The requested page could not be found.",
       canonical_url=abs_url(request.path),robots_meta="noindex,follow"),404

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")),debug=False)
