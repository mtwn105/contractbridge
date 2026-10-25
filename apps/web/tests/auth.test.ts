import assert from "node:assert/strict";
import { test } from "node:test";
import { matchesToken, newSession, sameOrigin, validSession } from "../lib/auth.ts";

process.env.OPERATOR_TOKEN = "ephemeral-unit-test-token-at-least-32-characters";

test("operator token comparison handles incorrect and Unicode input", () => {
  assert.equal(matchesToken(process.env.OPERATOR_TOKEN!), true);
  assert.equal(matchesToken("incorrect"), false);
  assert.equal(matchesToken("🔑"), false);
});
test("session signatures reject tampering, malformed values, and trailing data", () => {
  const session = newSession();
  assert.equal(validSession(session), true);
  assert.equal(validSession("a" + session), false);
  assert.equal(validSession(session + ".extra"), false);
  assert.equal(validSession("not-a-session"), false);
});
test("mutation origin must match host and protocol", () => {
  const req = (origin: string, host = "localhost:3000") => new Request("http://localhost:3000/api/session", { headers: { Origin: origin, Host: host } });
  assert.equal(sameOrigin(req("http://localhost:3000")), true);
  assert.equal(sameOrigin(req("https://evil.example")), false);
  assert.equal(sameOrigin(req("https://localhost:3000")), false);
  assert.equal(sameOrigin(new Request("http://localhost:3000/api/session")), false);
});
