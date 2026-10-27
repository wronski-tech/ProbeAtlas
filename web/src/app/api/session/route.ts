import { NextRequest, NextResponse } from "next/server";
import { API_URL, SESSION_COOKIE, WORKSPACE_COOKIE, backend, cookieOptions } from "@/lib/server";

/** Reject requests without the CSRF header, which cross-site forms and simple requests cannot send. */
function csrfRejected(req: NextRequest) {
  if (req.headers.get("x-probeatlas-csrf") === "1") return null;
  return NextResponse.json({ error: { code: "csrf", message: "missing CSRF header" } }, { status: 403 });
}

/**
 * Sign in or register; the session token is stored in an httpOnly cookie, never exposed to JS.
 * Requires the CSRF header so another site cannot sign the browser into an attacker's account.
 */
export async function POST(req: NextRequest) {
  const rejected = csrfRejected(req);
  if (rejected) return rejected;
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

/** Sign out: revoke the session in the API (best effort) and clear both cookies. */
export async function DELETE(req: NextRequest) {
  const rejected = csrfRejected(req);
  if (rejected) return rejected;
  await backend("/v1/auth/logout", { method: "POST" }).catch(() => undefined);
  const out = NextResponse.json({ ok: true });
  out.cookies.delete(SESSION_COOKIE);
  out.cookies.delete(WORKSPACE_COOKIE);
  return out;
}
