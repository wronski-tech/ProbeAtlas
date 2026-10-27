import { NextRequest, NextResponse } from "next/server";
import { backend } from "@/lib/server";

/**
 * Same-origin proxy from the browser to the public API (/v1 only).
 * Mutating requests must carry `x-probeatlas-csrf: 1`; browsers cannot add
 * custom headers to cross-site requests without a CORS preflight we never allow.
 */
async function handle(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  if (path[0] !== "v1" || path.some((p) => p === ".." || p === "")) {
    return NextResponse.json({ error: { code: "not_found", message: "not found" } }, { status: 404 });
  }
  if (req.method !== "GET" && req.headers.get("x-probeatlas-csrf") !== "1") {
    return NextResponse.json({ error: { code: "csrf", message: "missing CSRF header" } }, { status: 403 });
  }
  const init: RequestInit = { method: req.method, headers: { "Content-Type": "application/json" } };
  if (req.method !== "GET" && req.method !== "DELETE") init.body = await req.text();
  const res = await backend(`/${path.join("/")}${req.nextUrl.search}`, init);
  const body = res.status === 204 ? null : await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "application/json",
      ...(res.headers.get("Retry-After") ? { "Retry-After": res.headers.get("Retry-After")! } : {}),
    },
  });
}

export { handle as GET, handle as POST, handle as PATCH, handle as DELETE };
