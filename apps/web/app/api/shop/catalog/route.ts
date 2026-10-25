import { shoppingProxy } from "@/lib/shopping-proxy";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request) { return shoppingProxy(request, "catalog"); }
