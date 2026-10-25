import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import { COOKIE, matchesToken, newSession, sameOrigin, validSession } from "@/lib/auth";

export async function GET() {
  return NextResponse.json({ authenticated: validSession((await cookies()).get(COOKIE)?.value) }, { headers: { "Cache-Control": "no-store" } });
}
export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return NextResponse.json({ title: "Invalid request origin" }, { status: 403 });
  if (Number(request.headers.get("content-length") || 0) > 2048) return new NextResponse(null, { status: 413 });
  let value;
  try { value = await request.json(); } catch { return new NextResponse(null, { status: 400 }); }
  if (typeof value.token !== "string" || value.token.length > 512 || !matchesToken(value.token))
    return NextResponse.json({ title: "Invalid operator token" }, { status: 401 });
  const response = NextResponse.json({ authenticated: true });
  response.cookies.set(COOKIE, newSession(), { httpOnly: true, sameSite: "strict", secure: request.nextUrl.protocol === "https:", path: "/", maxAge: 8 * 3600 });
  return response;
}
export async function DELETE(request: NextRequest) {
  if (!sameOrigin(request)) return new NextResponse(null, { status: 403 });
  const response = NextResponse.json({ authenticated: false }); response.cookies.delete(COOKIE); return response;
}
