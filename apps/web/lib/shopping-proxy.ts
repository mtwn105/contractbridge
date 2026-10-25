import { sameOrigin } from "./auth";

async function limitedText(body: ReadableStream<Uint8Array> | null) {
  if (!body) return "";
  const reader = body.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 16_384) { await reader.cancel(); throw new Error("Body too large"); }
      parts.push(value);
    }
  } finally { reader.releaseLock(); }
  return Buffer.concat(parts).toString("utf8");
}

export async function shoppingProxy(request: Request, operation: "catalog" | "checkout") {
  const headers = { "Cache-Control": "no-store", "Content-Type": "application/json" };
  const problem = (title: string, status: number) => Response.json({ title }, { status, headers });
  if (operation === "checkout" && !sameOrigin(request)) return problem("Invalid request origin", 403);
  const token = process.env.SHOP_API_TOKEN;
  if (!token || token.length < 24) return problem("The shop is not configured yet.", 503);
  let data: string | undefined;
  if (operation === "checkout") {
    if (request.headers.get("content-type")?.split(";")[0] !== "application/json") return problem("Expected a JSON cart", 415);
    try { data = await limitedText(request.body); } catch { return problem("Cart is too large", 413); }
  }
  try {
    const response = await fetch(`${process.env.SHOP_API_URL || "http://127.0.0.1:8001"}/${operation}`, {
      method: operation === "checkout" ? "POST" : "GET", body: data, cache: "no-store", signal: AbortSignal.timeout(20_000),
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, redirect: "error"
    });
    return new Response(await limitedText(response.body), { status: response.status, headers });
  } catch { return problem("We could not confirm your order. The request was not retried.", 503); }
}
