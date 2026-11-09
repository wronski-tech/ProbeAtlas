"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button, ErrorBanner, Field, Input } from "./ui";

/** Where to go after signing in: a same-site path from `?next=` (for example an invitation), else the dashboard. */
function nextPath(): string {
  const next = new URLSearchParams(window.location.search).get("next");
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/";
}

/** Sign-in or registration form. The session token is kept in an httpOnly cookie by the server route. */
export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Read after mount (window is not available during prerendering); kept when switching login ↔ register.
  const [nextQuery, setNextQuery] = useState("");
  // Registering from an invitation link joins that workspace (required when sign-up is invite-only).
  const [invitation, setInvitation] = useState<string | null>(null);
  useEffect(() => {
    const next = nextPath();
    if (next !== "/") setNextQuery(`?next=${encodeURIComponent(next)}`);
    setInvitation(/^\/invite\/(pai_[A-Za-z0-9_-]+)$/.exec(next)?.[1] ?? null);
  }, []);
  const joining = mode === "register" && invitation !== null;

  /** Submit credentials to the session route and go to the dashboard on success. */
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const form = Object.fromEntries(new FormData(e.currentTarget));
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-probeatlas-csrf": "1" },
      body: JSON.stringify({ action: mode, ...form, ...(joining ? { invitation_token: invitation } : {}) }),
    });
    const data = await res.json().catch(() => ({}));
    setBusy(false);
    if (!res.ok) {
      const err = data?.error;
      setError(err?.details?.map((d: { loc: string[]; msg: string }) => `${d.loc.at(-1)}: ${d.msg}`).join("; ") || err?.message || "Request failed");
      return;
    }
    // A new account made from an invitation is already a member: go to the joined workspace.
    router.push(joining ? "/" : nextPath());
    router.refresh();
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold">ProbeAtlas</div>
          <p className="mt-1 text-sm text-slate-500">{mode === "login" ? "Sign in to your workspace" : joining ? "Create your account to join the workspace you were invited to" : "Create your account"}</p>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-lg border border-slate-200 bg-white p-6">
          {mode === "register" && (
            <>
              <Field label="Name">
                <Input name="name" required maxLength={100} autoComplete="name" />
              </Field>
              {!joining && (
                <Field label="Workspace name">
                  <Input name="workspace_name" required defaultValue="My workspace" maxLength={100} />
                </Field>
              )}
            </>
          )}
          <Field label="Email">
            <Input name="email" type="email" required autoComplete="email" />
          </Field>
          <Field label="Password" hint={mode === "register" ? "At least 10 characters." : undefined}>
            <Input name="password" type="password" required minLength={mode === "register" ? 10 : undefined} autoComplete={mode === "login" ? "current-password" : "new-password"} />
          </Field>
          <ErrorBanner error={error} />
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>
        <p className="mt-4 text-center text-sm text-slate-500">
          {mode === "login" ? (
            <>
              No account? <Link className="font-medium text-sky-700 hover:underline" href={`/register${nextQuery}`}>Register</Link>
            </>
          ) : (
            <>
              Already registered? <Link className="font-medium text-sky-700 hover:underline" href={`/login${nextQuery}`}>Sign in</Link>
            </>
          )}
        </p>
      </div>
    </main>
  );
}
