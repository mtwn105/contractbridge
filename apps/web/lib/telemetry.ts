import type { Event, StreamedSpanJSON } from "@sentry/core";

const verificationMessages = new Set([
  "ContractBridge web monitoring verification",
  "ContractBridge browser monitoring verification",
]);

export function scrubTelemetry<T extends Event>(event: T): T {
  delete event.request;
  delete event.user;
  delete event.breadcrumbs;
  delete event.extra;
  delete event.message;
  delete event.logentry;
  for (const exception of event.exception?.values || []) {
    if (!verificationMessages.has(exception.value || "")) exception.value = "Exception details withheld by ContractBridge privacy policy";
    for (const frame of exception.stacktrace?.frames || []) delete frame.vars;
  }
  return event;
}

export const privateData = {
  userInfo: false, cookies: false, httpHeaders: false, httpBodies: [],
  urlQueryParams: false, genAI: { inputs: false, outputs: false },
  mcp: { inputs: false, outputs: false }, databaseQueryData: false,
  queues: false, stackFrameVariables: false,
} as const;

export function scrubSpan(span: StreamedSpanJSON): StreamedSpanJSON {
  // SDK metadata remains useful without request bodies, headers, or URL parameters.
  const safeKeys = new Set(["http.request.method", "http.response.status_code", "http.route", "sentry.op", "sentry.origin", "sentry.source", "sentry.status", "sentry.kind", "sentry.sample_rate", "sentry.segment.name.source", "server.address", "server.port"]);
  for (const key of Object.keys(span.attributes || {})) {
    if (!safeKeys.has(key)) delete span.attributes[key];
  }
  return span;
}
