import { NextRequest, NextResponse } from "next/server";
import { API_URL, SESSION_COOKIE, WORKSPACE_COOKIE, backend, cookieOptions } from "@/lib/server";

/** Sign in or register; the session token is stored in an httpOnly cookie, never exposed to JS. */
export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const action = body.action === "register" ? "register" : "login";
  const { action: _ignored, ...payload } = body;
  const res = await fetch(`${API_URL}/v1/auth/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) return NextResponse.json(data, { status: res.status });
  const out = NextResponse.json({ user: data.user, workspaces: data.workspaces });
  const maxAge = Math.max(0, Math.floor((new Date(data.expires_at).getTime() - Date.now()) / 1000));
  out.cookies.set(SESSION_COOKIE, data.token, { ...cookieOptions, maxAge });
  out.cookies.set(WORKSPACE_COOKIE, data.workspaces[0]?.id ?? "", { ...cookieOptions, maxAge });
  return out;
}

export async function DELETE() {
  await backend("/v1/auth/logout", { method: "POST" }).catch(() => undefined);
  const out = NextResponse.json({ ok: true });
  out.cookies.delete(SESSION_COOKIE);
  out.cookies.delete(WORKSPACE_COOKIE);
  return out;
}
