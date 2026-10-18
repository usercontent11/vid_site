import { defineConfig } from "@neon/config/v1";

const siteBaseUrl = process.env.SITE_BASE_URL || "https://mobilexh.live";
const inspectorToken = process.env.NEON_INSPECT_TOKEN;
if (!inspectorToken) {
  throw new Error("NEON_INSPECT_TOKEN must be supplied when deploying the source page inspector.");
}

export default defineConfig({
  functions: {
    sourcepageinspect: {
      name: "Source page inspector",
      source: "./neon-functions/source-page-inspect.ts",
      env: {
        SITE_BASE_URL: siteBaseUrl,
        NEON_INSPECT_TOKEN: inspectorToken,
        ...(process.env.SOURCE_COOKIE ? { SOURCE_COOKIE: process.env.SOURCE_COOKIE } : {}),
      },
    },
  },
});
