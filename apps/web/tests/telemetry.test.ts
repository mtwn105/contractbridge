import { strict as assert } from "node:assert";
import { test } from "node:test";
import { scrubTelemetry, privateData } from "../lib/telemetry.ts";

test("Sentry removes private payloads while retaining diagnostic frames", () => {
  const secret = "private-payload-and-token";
  const event = { request: { data: secret }, user: { email: secret }, breadcrumbs: [{ message: secret }], extra: { input: secret }, message: secret,
    exception: { values: [{ type: "Error", value: secret, stacktrace: { frames: [{ filename: "page.tsx", function: "render", lineno: 10, vars: { input: secret } }] } }] } };
  const result = scrubTelemetry(event);
  assert(!JSON.stringify(result).includes(secret));
  assert.equal(result.exception.values[0].stacktrace.frames[0].filename, "page.tsx");
  assert.equal(privateData.cookies, false);
  assert.deepEqual(privateData.httpBodies, []);
  assert.equal(privateData.genAI.inputs, false);
});
