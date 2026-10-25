import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs/config";
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async headers() { return [{ source: "/:path*", headers: [
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "same-origin" },
    { key: "X-Frame-Options", value: "DENY" }
  ] }]; }
};
export default withSentryConfig(config, {
  org: process.env.SENTRY_ORG, project: process.env.SENTRY_PROJECT || "contractbridge-web", silent: true,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  sourcemaps: { disable: process.env.SENTRY_AUTH_TOKEN && process.env.SENTRY_ORG ? false : "disable-upload" },
  telemetry: false,
});
