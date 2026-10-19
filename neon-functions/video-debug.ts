const DEFAULT_BASE_URL = "https://mobilexh.live";
const MAX_HTML_BYTES = 4 * 1024 * 1024;
const DEFAULT_PREFERENCE_COOKIE = [
  "cookie_accept_v2=%7B%22e%22%3A1%2C%22f%22%3A1%2C%22t%22%3A1%2C%22a%22%3A1%7D",
  "x_content_preference_index=straight",
  "parental-control=yes",
  "stats_src_last=mobilexh.live",
  "x_csrf_token=1",
  "x_viewes=%5B9981710%2C9996409%5D",
  "h_v4_straight=%7B%22v%22%3A%5B%5D%2C%22l%22%3A%5B%5D%2C%22f%22%3A%5B%5D%2C%22pv%22%3A%5B9996409%2C9981710%5D%7D",
  "ff_thumb_offset=6",
  "moments_listing_ad_offset=6",
  "settings=eyJpc1dlYnBTdXBwb3J0ZWQiOnRydWUsImlzV2VibVN1cHBvcnRlZCI6dHJ1ZSwiZXh0RGV0ZWN0ZWRWMiI6ZmFsc2UsIm1vbWVudHNJc0hpZGRlbiI6bnVsbCwiaXNTaWRlYmFySGlkZGVuIjpudWxsLCJleHBpcmVzIjp7ImV4dERldGVjdGVkVjIiOjE3OTIyNTAyMTB9LCJ0c1Nwb3RDb3VudGVycyI6W3sic3BvdCI6Im1hc3Rlcl9jdWJlIiwidGltZSI6MTc5MjI5MTEyNCwiY291bnQiOjN9LHsic3BvdCI6Im1hc3Rlcl9mb290ZXIiLCJ0aW1lIjoxNzkyMjkxMTI0LCJjb3VudCI6NX0seyJzcG90IjoibmF0aXZlX3RodW1iIiwidGltZSI6MTc5MjI5MTM4NSwiY291bnQiOjF9XX0%3D",
  "recs_show_time=1792294321",
  "u-v-channels=straight%3A424516-2x273-1x43949-1x425459-1",
].join("; ");

function cookieHeader(rawValue: string | undefined): string {
  const raw = (rawValue ?? "").trim();
  if (!raw) return DEFAULT_PREFERENCE_COOKIE;

  if (raw.startsWith("{")) {
    try {
      const value = JSON.parse(raw);
      const required = ["i_l", "i_ll", "i_b", "i_e", "i_et"];
      if (value && typeof value === "object" && required.every((key) => key in value)) {
        return "g_state=" + encodeURIComponent(JSON.stringify(value));
      }
    } catch {
      // Fall through to the header/export parser.
    }
  }

  if (raw.includes("\t") && raw.includes("\n")) {
    const pairs: string[] = [];
    for (const line of raw.split(/\r?\n/)) {
      if (!line || line.startsWith("#")) continue;
      const parts = line.split("\t");
      if (parts.length < 7) continue;
      const name = parts[5]?.trim();
      const value = parts.slice(6).join("\t").trim();
      if (name) pairs.push(name + "=" + value);
    }
    return pairs.length ? pairs.join("; ") : DEFAULT_PREFERENCE_COOKIE;
  }

  return raw.replace(/^cookie:\s*/i, "").trim() || DEFAULT_PREFERENCE_COOKIE;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store, max-age=0",
      "x-content-type-options": "nosniff",
      "x-robots-tag": "noindex, nofollow",
    },
  });
}

async function readBounded(response: Response): Promise<{ text: string; bytes: number }> {
  const declared = Number(response.headers.get("content-length") ?? 0);
  if (declared > MAX_HTML_BYTES) throw new Error("Source page response exceeded the size limit.");
  if (!response.body) return { text: "", bytes: 0 };
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    total += item.value.byteLength;
    if (total > MAX_HTML_BYTES) {
      await reader.cancel();
      throw new Error("Source page response exceeded the size limit.");
    }
    chunks.push(item.value);
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { text: new TextDecoder().decode(joined), bytes: total };
}

function validateTarget(value: string, baseUrl: string): URL {
  const base = new URL(baseUrl);
  const target = new URL(value);
  if (base.protocol !== "https:" || target.protocol !== "https:"
      || target.origin !== base.origin || target.username || target.password
      || !target.pathname.startsWith("/videos/") || target.search || target.hash) {
    throw new Error("Only same-origin HTTPS /videos/ pages are allowed.");
  }
  return target;
}

async function fetchSource(start: URL, cookie: string): Promise<{ response: Response; finalUrl: URL }> {
  let current = start;
  for (let attempt = 0; attempt < 2; attempt++) {
    const response = await fetch(current, {
      method: "GET",
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
        "Cookie": cookie,
      },
      redirect: "manual",
      signal: AbortSignal.timeout(18_000),
    });
    if (response.status < 300 || response.status >= 400) return { response, finalUrl: current };
    const location = response.headers.get("location");
    if (!location || attempt > 0) throw new Error("Source redirect limit reached.");
    const next = new URL(location, current);
    if (next.protocol !== "https:" || next.origin !== start.origin
        || next.username || next.password || !next.pathname.startsWith("/videos/")) {
      throw new Error("Source redirected outside the allowed video page path.");
    }
    current = next;
  }
  throw new Error("Source redirect limit reached.");
}

function extractMediaLinks(html: string, pageUrl: string): string[] {
  const normalized = html
    .replace(/\\\//g, "/")
    .replace(/\\u002f/gi, "/")
    .replace(/\\u0026/gi, "&")
    .replace(/\\u003d/gi, "=")
    .replace(/&amp;/gi, "&");
  const urls = new Set<string>();
  const expression = /https?:\/\/[^\s"'<>\\]+/gi;
  for (const match of normalized.matchAll(expression)) {
    const candidate = match[0].replace(/[),;\]}]+$/g, "");
    try {
      const parsed = new URL(candidate, pageUrl);
      if (parsed.protocol !== "https:" || !parsed.hostname || parsed.username || parsed.password) continue;
      if (!/\.(?:m3u8|mp4|m4v|webm|mpd)$/i.test(parsed.pathname)) continue;
      urls.add(parsed.href);
      if (urls.size >= 100) break;
    } catch {
      // Ignore malformed URLs in the page.
    }
  }
  return [...urls];
}

export default {
  async fetch(request: Request): Promise<Response> {
    if (request.method !== "GET") return json({ error: "Method not allowed." }, 405);
    const baseUrl = (process.env.SITE_BASE_URL || DEFAULT_BASE_URL).trim().replace(/\/+$/, "");
    let target: URL;
    try {
      const supplied = new URL(request.url).searchParams.get("url") || "";
      target = validateTarget(supplied, baseUrl);
    } catch {
      return json({ ok: false, error: "Supply a same-origin HTTPS /videos/ URL using the url query parameter." }, 400);
    }

    try {
      const cookie = cookieHeader(process.env.SOURCE_COOKIE);
      const { response, finalUrl } = await fetchSource(target, cookie);
      const status = response.status;
      const contentType = response.headers.get("content-type");
      if (!response.ok) {
        await response.body?.cancel();
        return json({
          ok: false, page: target.href, finalUrl: finalUrl.href, status,
          contentType, bytesRead: 0, count: 0, links: [], finalHost: finalUrl.hostname,
          note: "The source page did not return a successful response.",
        }, 502);
      }
      const body = await readBounded(response);
      const links = extractMediaLinks(body.text, finalUrl.href);
      return json({
        ok: true,
        page: target.href,
        finalUrl: finalUrl.href,
        status,
        contentType,
        bytesRead: body.bytes,
        count: links.length,
        links,
        note: "Found URLs in returned HTML/text only. JavaScript-loaded streams may require browser Network inspection.",
        finalHost: finalUrl.hostname,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown source-page error.";
      return json({ ok: false, error: message, count: 0, links: [] }, 502);
    }
  },
};
