import { cookies, headers as requestHeaders } from "next/headers";
import { clientIpFromForwardedFor } from "./proxy";
import type { Location } from "./types";

export const API_URL = process.env.PROBEATLAS_API_URL ?? "http://localhost:8000";
export const SESSION_COOKIE = "pa_session";
export const WORKSPACE_COOKIE = "pa_workspace";
/** Reverse proxies in front of this server that append to X-Forwarded-For (0 = browsers connect directly). */
const TRUSTED_PROXY_HOPS = Number(process.env.PROBEATLAS_WEB_TRUSTED_PROXY_HOPS ?? "0");

export type Workspace = { id: string; name: string; role: string | null };
export type Me = { user: { id: string; email: string; name: string }; workspaces: Workspace[] };

/** Cookie settings for the session and workspace cookies: httpOnly, SameSite=Lax, Secure in production. */
export const cookieOptions = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production" && process.env.PROBEATLAS_INSECURE_COOKIES !== "1",
  path: "/",
};

/**
 * Headers that tell the API who the browser is: `X-Forwarded-For` with the client IP when it can be
 * trusted. The API honors it only from addresses in `PROBEATLAS_TRUSTED_PROXIES`, so per-IP rate limits and
 * audit events see the real client instead of this server.
 */
export async function forwardingHeaders(): Promise<Record<string, string>> {
  const ip = clientIpFromForwardedFor((await requestHeaders()).get("x-forwarded-for"), TRUSTED_PROXY_HOPS);
  return ip ? { "X-Forwarded-For": ip } : {};
}

/** Server-side call to the control plane using the caller's session cookie. */
export async function backend(path: string, init: RequestInit = {}): Promise<Response> {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value;
  const workspace = jar.get(WORKSPACE_COOKIE)?.value;
  const headers = new Headers(init.headers);
  for (const [k, v] of Object.entries(await forwardingHeaders())) headers.set(k, v);
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

/**
 * Public contact address for access requests and abuse reports (`PROBEATLAS_CONTACT_EMAIL`), or null when the
 * operator has not configured one. Read at request time, so pages that use it must render dynamically.
 */
export function contactEmail(): string | null {
  return process.env.PROBEATLAS_CONTACT_EMAIL?.trim() || null;
}

/** Whether the browser sends a session cookie. Not validated: only picks "Open dashboard" over "Sign in". */
export async function hasSessionCookie(): Promise<boolean> {
  return (await cookies()).has(SESSION_COOKIE);
}

/**
 * The monitoring locations this deployment runs, from the public API, or null when the API is unreachable.
 * Cached for 30 seconds so visitors share one upstream request (the endpoint is rate limited per IP).
 */
export async function publicLocations(): Promise<Location[] | null> {
  try {
    const res = await fetch(`${API_URL}/v1/public/locations`, { next: { revalidate: 30 } });
    return res.ok ? ((await res.json()) as Location[]) : null;
  } catch {
    return null;
  }
}
