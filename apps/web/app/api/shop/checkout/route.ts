import { shoppingProxy } from "@/lib/shopping-proxy";
export const runtime = "nodejs";
export async function POST(request: Request) { return shoppingProxy(request, "checkout"); }
