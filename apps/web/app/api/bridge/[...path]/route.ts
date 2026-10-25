import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import { COOKIE, sameOrigin, validSession } from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  if (!validSession((await cookies()).get(COOKIE)?.value)) return NextResponse.json({ title: "Sign in to the workspace" }, { status: 401 });
  if (request.method !== "GET" && !sameOrigin(request)) return NextResponse.json({ title: "Invalid request origin" }, { status: 403 });
  const { path } = await context.params;
  if (path.some(s => !/^[a-zA-Z0-9_-]+$/.test(s))) return new NextResponse(null, { status: 400 });
  const data = request.method === "GET" ? undefined : await request.text();
  if (data && Buffer.byteLength(data) > 1_000_000) return new NextResponse(null, { status: 413 });
  try {
    const upstream = await fetch(`${process.env.CONTROL_PLANE_URL || "http://127.0.0.1:8080"}/api/v1/${path.join("/")}`, {
      method: request.method, body: data, cache: "no-store", signal: AbortSignal.timeout(10000),
      headers: { Authorization: `Bearer ${process.env.OPERATOR_TOKEN}`, "Content-Type": "application/json",
                 ...(request.headers.has("if-match") ? { "If-Match": request.headers.get("if-match")! } : {}) }
    });
    return new NextResponse(upstream.body, { status: upstream.status, headers: {
      "Content-Type": upstream.headers.get("content-type") || "application/json", "Cache-Control": "no-store",
      ...(upstream.headers.has("etag") ? { ETag: upstream.headers.get("etag")! } : {}),
      ...(upstream.headers.has("content-disposition") ? { "Content-Disposition": upstream.headers.get("content-disposition")! } : {})
    } });
  } catch { return NextResponse.json({ title: "Control plane is unavailable. Check the backend service." }, { status: 503 }); }
}
export { forward as GET, forward as POST, forward as PUT };
