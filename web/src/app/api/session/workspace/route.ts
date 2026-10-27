import { NextRequest, NextResponse } from "next/server";
import { WORKSPACE_COOKIE, cookieOptions, sessionContext } from "@/lib/server";

/** Switch the active workspace after verifying membership. */
export async function POST(req: NextRequest) {
  if (req.headers.get("x-probeatlas-csrf") !== "1") {
    return NextResponse.json({ error: { code: "csrf", message: "missing CSRF header" } }, { status: 403 });
  }
  const { workspace_id } = await req.json().catch(() => ({}));
  const ctx = await sessionContext();
  if (!ctx) return NextResponse.json({ error: { code: "unauthorized", message: "sign in" } }, { status: 401 });
  if (!ctx.me.workspaces.some((w) => w.id === workspace_id)) {
    return NextResponse.json({ error: { code: "not_found", message: "workspace not found" } }, { status: 404 });
  }
  const out = NextResponse.json({ ok: true });
  out.cookies.set(WORKSPACE_COOKIE, workspace_id, cookieOptions);
  return out;
}
