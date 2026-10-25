import * as Sentry from "@sentry/nextjs";
import { privateData, scrubSpan, scrubTelemetry } from "./lib/telemetry";

const environment = process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT || "development";
Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  enabled: Boolean(process.env.NEXT_PUBLIC_SENTRY_DSN),
  environment, release: process.env.NEXT_PUBLIC_SENTRY_RELEASE || "contractbridge@0.1.0",
  tracesSampleRate: environment === "production" ? 0.1 : 1,
  dataCollection: { ...privateData, httpBodies: [] },
  beforeSend: scrubTelemetry, beforeSendSpan: scrubSpan,
  beforeBreadcrumb: () => null,
  tracePropagationTargets: [/^\//],
});
export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
