import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("Pass the compiled Neon Function module path.");
const loaded = await import(pathToFileURL(modulePath).href);
const handler = loaded.default;

process.env.NEON_INSPECT_TOKEN = "test-only-inspector-token";
process.env.SITE_BASE_URL = "https://mobilexh.live";
process.env.SOURCE_COOKIE = "";

let calls = 0;
const originalFetch = globalThis.fetch;
const fixture = [
  "<!doctype html><html><head><title>Test source page</title>",
  '<link rel="preload" as="fetch" crossorigin href="https://media.example/master?token=SHOULD_NOT_LEAK">',
  "</head><body>",
  '<script id="initials-script">window.initials = {"videoModel":{"id":123,"title":"Example","downloadFile":"/movies/test/download/144p"},"urls":{"hls":"https://static.cdnsolutions.media/xh-shared/js/hls.js?api_key=SHOULD_NOT_LEAK"}};</script>',
  '<script id="appcontext-script">window.context = {"player":true,"media":{}};</script>',
  "</body></html>",
].join("");

globalThis.fetch = async (input, init) => {
  calls += 1;
  assert.equal(new URL(String(input)).origin, "https://mobilexh.live");
  assert.equal(new URL(String(input)).pathname, "/videos/test");
  assert.equal(init.redirect, "manual");
  assert.match(new Headers(init.headers).get("cookie"), /parental-control=yes/);
  return new Response(fixture, {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
};

try {
  const unauthorized = await handler.fetch(new Request(
    "https://function.example/?path=%2Fvideos%2Ftest",
    { headers: { authorization: "Bearer wrong-token" } },
  ));
  assert.equal(unauthorized.status, 401);
  assert.equal(calls, 0, "Unauthorized requests must not fetch the source.");

  const response = await handler.fetch(new Request(
    "https://function.example/?path=%2Fvideos%2Ftest",
    { headers: { authorization: "Bearer test-only-inspector-token" } },
  ));
  assert.equal(response.status, 200);
  const report = await response.json();

  assert.equal(report.fetch.ok, true);
  assert.equal(report.page.title, "Test source page");
  assert.equal(report.initials.videoModelType, "object");
  assert.ok(report.initials.videoModelKeys.includes("downloadFile"));
  assert.equal(report.initials.downloadFile.path, "/movies/test/download/144p");
  assert.equal(report.initials.hlsUrl.host, "static.cdnsolutions.media");
  assert.equal(report.initials.hlsUrl.path, "/xh-shared/js/hls.js");
  assert.equal(report.rawHtmlReturned, false);
  assert.equal(report.secretsReturned, false);

  const output = JSON.stringify(report);
  assert.ok(!output.includes("SHOULD_NOT_LEAK"));
  assert.ok(!output.includes("test-only-inspector-token"));
  console.log("Neon Function inspector tests passed.");
} finally {
  globalThis.fetch = originalFetch;
}
