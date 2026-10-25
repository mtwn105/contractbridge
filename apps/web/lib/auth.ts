import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const COOKIE = "cb_session";
function secret() {
  const token = process.env.OPERATOR_TOKEN;
  if (!token || token.length < 24) throw new Error("OPERATOR_TOKEN must be configured");
  return token;
}
export function matchesToken(value: string) {
  const a = createHmac("sha256", secret()).update(value).digest();
  const b = createHmac("sha256", secret()).update(secret()).digest();
  return timingSafeEqual(a, b);
}
export function newSession() {
  const value = Buffer.from(JSON.stringify({ exp: Date.now() + 8 * 3600000, nonce: randomBytes(16).toString("hex") })).toString("base64url");
  return value + "." + createHmac("sha256", secret()).update(value).digest("base64url");
}
export function validSession(value?: string) {
  if (!value || value.length > 512) return false;
  const parts = value.split(".");
  if (parts.length !== 2) return false;
  const [payload, signature] = parts;
  if (!payload || !signature) return false;
  const expected = createHmac("sha256", secret()).update(payload).digest();
  const actual = Buffer.from(signature, "base64url");
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return false;
  try { const decoded = JSON.parse(Buffer.from(payload, "base64url").toString()); return decoded.exp > Date.now(); }
  catch { return false; }
}
export function sameOrigin(request: Request) {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try { return new URL(origin).host === request.headers.get("host") && new URL(origin).protocol === new URL(request.url).protocol; }
  catch { return false; }
}
