import { cookies } from "next/headers";

export const API_URL = process.env.PROBEATLAS_API_URL ?? "http://localhost:8000";
export const SESSION_COOKIE = "pa_session";
export const WORKSPACE_COOKIE = "pa_workspace";

export type Workspace = { id: string; name: string; role: string | null };
export type Me = { user: { id: string; email: string; name: string }; workspaces: Workspace[] };

export const cookieOptions = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production" && process.env.PROBEATLAS_INSECURE_COOKIES !== "1",
  path: "/",
};

/** Server-side call to the control plane using the caller's session cookie. */
export async function backend(path: string, init: RequestInit = {}): Promise<Response> {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value;
  const workspace = jar.get(WORKSPACE_COOKIE)?.value;
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (workspace) headers.set("X-Workspace-Id", workspace);
  return fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
}

/** Current user and selected workspace, or null when not signed in. */
export async function sessionContext(): Promise<{ me: Me; workspace: Workspace } | null> {
  const jar = await cookies();
  if (!jar.get(SESSION_COOKIE)) return null;
  const res = await backend("/v1/auth/me");
  if (!res.ok) return null;
  const me = (await res.json()) as Me;
  const selected = jar.get(WORKSPACE_COOKIE)?.value;
  const workspace = me.workspaces.find((w) => w.id === selected) ?? me.workspaces[0];
  if (!workspace) return null;
  return { me, workspace };
}
