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
].join("; ")

type SafeUrl = {
  scheme: string;
  host: string;
  path: string;
  hasQuery: boolean;
  queryParameterNames: string[];
};

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

function equalSecret(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

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

async function readTextBounded(response: Response): Promise<{ text: string; bytes: number }> {
  const declaredLength = Number(response.headers.get("content-length") ?? 0);
  if (declaredLength > MAX_HTML_BYTES) {
    throw new Error("Source page response exceeded the configured size limit.");
  }
  if (!response.body) return { text: "", bytes: 0 };

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    total += part.value.byteLength;
    if (total > MAX_HTML_BYTES) {
      await reader.cancel();
      throw new Error("Source page response exceeded the configured size limit.");
    }
    chunks.push(part.value);
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { text: new TextDecoder().decode(joined), bytes: total };
}

function summarizeUrl(value: unknown, baseUrl: string): SafeUrl | null {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = new URL(value, baseUrl);
    const queryParameterNames = [...new Set([...parsed.searchParams.keys()])].sort().slice(0, 40);
    return {
      scheme: parsed.protocol.replace(/:$/, ""),
      host: parsed.hostname,
      path: parsed.pathname.slice(0, 320),
      hasQuery: Boolean(parsed.search),
      queryParameterNames,
    };
  } catch {
    return null;
  }
}

function extractInitials(html: string): { present: boolean; parseError?: boolean; data?: Record<string, unknown>; chars: number } {
  const match = html.match(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)
    ?.map((element) => {
      const attrs = element.match(/^<script\b([^>]*)>/i)?.[1] ?? "";
      const body = element.replace(/^<script\b[^>]*>/i, "").replace(/<\/script\s*>$/i, "");
      return { attrs, body, id: (attrs.match(/\bid\s*=\s*["']([^"']+)["']/i)?.[1] ?? "") };
    })
    .find((script) => script.id === "initials-script");

  if (!match) return { present: false, chars: 0 };
  let raw = match.body.trim().replace(/^\s*window\.initials\s*=\s*/, "").trim();
  if (raw.endsWith(";")) raw = raw.slice(0, -1);
  try {
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      return { present: true, parseError: true, chars: match.body.length };
    }
    return { present: true, data: data as Record<string, unknown>, chars: match.body.length };
  } catch {
    return { present: true, parseError: true, chars: match.body.length };
  }
}

function inspectHtml(html: string, pageUrl: string, status: number, contentType: string | null, responseBytes: number) {
  const initials = extractInitials(html);
  const data = initials.data ?? {};
  const videoModel = data.videoModel && typeof data.videoModel === "object"
    ? data.videoModel as Record<string, unknown>
    : null;
  const urls = data.urls && typeof data.urls === "object"
    ? data.urls as Record<string, unknown>
    : null;

  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)].map((match) => {
    const attrs = match[1] ?? "";
    const body = match[2] ?? "";
    const id = attrs.match(/\bid\s*=\s*["']([^"']+)["']/i)?.[1] ?? null;
    const src = attrs.match(/\bsrc\s*=\s*["']([^"']+)["']/i)?.[1] ?? null;
    const lower = body.toLowerCase();
    return {
      id,
      src: summarizeUrl(src, pageUrl),
      characters: body.length,
      contains: ["m3u8", "hls", "player", "master", "manifest", "media", "videoModel"]
        .filter((term) => lower.includes(term.toLowerCase())),
    };
  }).slice(0, 100);

  const links = [...html.matchAll(/<link\b([^>]*)>/gi)].map((match) => {
    const attrs = match[1] ?? "";
    const attr = (name: string) => attrs.match(new RegExp("\\b" + name + "\\s*=\\s*['\"]([^'\"]*)['\"]", "i"))?.[1] ?? "";
    return {
      rel: attr("rel").toLowerCase().split(/\s+/).filter(Boolean).slice(0, 12),
      as: attr("as").slice(0, 60),
      href: summarizeUrl(attr("href"), pageUrl),
      crossoriginAttributePresent: /\bcrossorigin(?:\s|=|$)/i.test(attrs),
    };
  }).slice(0, 100);

  const media = [...html.matchAll(/<(video|source|iframe|object|embed)\b([^>]*)>/gi)].map((match) => {
    const tag = (match[1] ?? "").toLowerCase();
    const attrs = match[2] ?? "";
    const attr = (name: string) => attrs.match(new RegExp("\\b" + name + "\\s*=\\s*['\"]([^'\"]*)['\"]", "i"))?.[1] ?? "";
    return {
      tag,
      src: summarizeUrl(attr("src"), pageUrl),
      dataUrl: summarizeUrl(attr("data"), pageUrl),
    };
  }).slice(0, 60);

  const title = html.match(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/i)?.[1]
    ?.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200) ?? "";

  return {
    fetch: { ok: status >= 200 && status < 300, status, contentType, responseBytes },
    page: {
      title,
      url: summarizeUrl(pageUrl, pageUrl),
      htmlCharacters: html.length,
      containsLiteralM3u8: /m3u8/i.test(html),
      m3u8Occurrences: (html.match(/m3u8/ig) ?? []).length,
    },
    initials: {
      present: initials.present,
      parseError: initials.parseError ?? false,
      scriptCharacters: initials.chars,
      topLevelKeys: Object.keys(data).sort().slice(0, 120),
      videoModelType: !("videoModel" in data) ? "missing" : videoModel ? "object" : data.videoModel === null ? "null" : typeof data.videoModel,
      videoModelKeys: videoModel ? Object.keys(videoModel).sort().slice(0, 120) : [],
      downloadFile: videoModel ? summarizeUrl(videoModel.downloadFile, pageUrl) : null,
      hlsUrl: urls ? summarizeUrl(urls.hls, pageUrl) : null,
    },
    keywordCounts: Object.fromEntries(
      ["m3u8", "videomodel", "player", "playlist", "hls", "master", "sources", "videourl", "stream", "manifest"]
        .map((term) => [term, html.toLowerCase().split(term).length - 1]),
    ),
    scriptCount: scripts.length,
    scripts,
    linkCount: links.length,
    links,
    mediaCount: media.length,
    media,
    secretsReturned: false,
    rawHtmlReturned: false,
  };
}

async function fetchPage(url: URL, headers: HeadersInit): Promise<{ response: Response; finalUrl: URL }> {
  let current = url;
  for (let attempt = 0; attempt < 2; attempt++) {
    const response = await fetch(current, {
      method: "GET",
      headers,
      redirect: "manual",
      signal: AbortSignal.timeout(15_000),
    });
    if (response.status < 300 || response.status >= 400) {
      return { response, finalUrl: current };
    }
    const location = response.headers.get("location");
    if (!location || attempt > 0) throw new Error("Source page redirect could not be followed safely.");
    const next = new URL(location, current);
    if (next.protocol !== "https:" || next.origin !== url.origin || next.username || next.password) {
      throw new Error("Source page redirected outside the configured HTTPS origin.");
    }
    current = next;
  }
  throw new Error("Source page redirect limit reached.");
}

export default {
  async fetch(request: Request): Promise<Response> {
    if (request.method !== "GET") return json({ error: "Method not allowed." }, 405);

    const token = process.env.NEON_INSPECT_TOKEN ?? "";
    if (!token) return json({ error: "Inspector token is not configured." }, 503);
    const authorization = request.headers.get("authorization") ?? "";
    const supplied = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
    if (!equalSecret(supplied, token)) return json({ error: "Unauthorized." }, 401);

    let base: URL;
    try {
      base = new URL(process.env.SITE_BASE_URL ?? DEFAULT_BASE_URL);
    } catch {
      return json({ error: "SITE_BASE_URL is invalid." }, 500);
    }
    if (base.protocol !== "https:" || base.username || base.password || base.search || base.hash) {
      return json({ error: "SITE_BASE_URL must be a clean HTTPS URL." }, 500);
    }
    base.pathname = base.pathname.replace(/\/+$/, "") + "/";

    const requestUrl = new URL(request.url);
    const pagePath = requestUrl.searchParams.get("path") ?? "/videos/xhfKO6H";
    if (!pagePath.startsWith("/videos/") || pagePath.startsWith("//") || /[?#\r\n\\]/.test(pagePath)) {
      return json({ error: "path must be a path-only /videos/... value." }, 400);
    }
    const target = new URL(pagePath.slice(1), base);
    if (target.origin !== base.origin || !target.pathname.startsWith("/videos/")) {
      return json({ error: "Requested source path is outside the configured video path." }, 400);
    }

    const headers: Record<string, string> = {
      "user-agent": "Mozilla/5.0 (compatible; VideoCatalogue/1.0)",
      "accept": "text/html,application/xhtml+xml",
      "accept-language": "en-US,en;q=0.8",
      "cookie": cookieHeader(process.env.SOURCE_COOKIE),
    };

    try {
      const { response, finalUrl } = await fetchPage(target, headers);
      let body: { text: string; bytes: number };
      try {
        body = await readTextBounded(response);
      } catch {
        return json({
          fetch: { ok: false, status: response.status, contentType: response.headers.get("content-type") },
          error: "Source response could not be read within the configured size limit.",
          secretsReturned: false,
          rawHtmlReturned: false,
        }, 502);
      }
      const report = inspectHtml(
        body.text,
        finalUrl.href,
        response.status,
        response.headers.get("content-type"),
        body.bytes,
      );
      return json(report, response.status >= 200 && response.status < 300 ? 200 : 502);
    } catch {
      return json({ error: "Could not fetch the source page safely.", secretsReturned: false }, 502);
    }
  },
};
