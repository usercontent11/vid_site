import { defineConfig } from "@neon/config/v1";

export default defineConfig({
  functions: {
    sourcepageinspect: {
      name: "Source page inspector",
      source: "./neon-functions/source-page-inspect.ts",
      env: {
        SITE_BASE_URL: process.env.SITE_BASE_URL || "https://mobilexh.live",
        SOURCE_COOKIE: process.env.SOURCE_COOKIE || "",
        NEON_INSPECT_TOKEN: process.env.NEON_INSPECT_TOKEN || "",
      },
    },
  },
});
