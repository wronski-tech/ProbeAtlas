"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, ErrorBanner, Field, Input } from "./ui";

/** Sign-in or registration form. The session token is kept in an httpOnly cookie by the server route. */
export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Submit credentials to the session route and go to the dashboard on success. */
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const form = Object.fromEntries(new FormData(e.currentTarget));
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-probeatlas-csrf": "1" },
      body: JSON.stringify({ action: mode, ...form }),
    });
    const data = await res.json().catch(() => ({}));
    setBusy(false);
    if (!res.ok) {
      const err = data?.error;
      setError(err?.details?.map((d: { loc: string[]; msg: string }) => `${d.loc.at(-1)}: ${d.msg}`).join("; ") || err?.message || "Request failed");
      return;
    }
    router.push("/");
    router.refresh();
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold">ProbeAtlas</div>
          <p className="mt-1 text-sm text-slate-500">{mode === "login" ? "Sign in to your workspace" : "Create your account"}</p>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-lg border border-slate-200 bg-white p-6">
          {mode === "register" && (
            <>
              <Field label="Name">
                <Input name="name" required maxLength={100} autoComplete="name" />
              </Field>
              <Field label="Workspace name">
                <Input name="workspace_name" required defaultValue="My workspace" maxLength={100} />
              </Field>
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
              No account? <Link className="font-medium text-sky-700 hover:underline" href="/register">Register</Link>
            </>
          ) : (
            <>
              Already registered? <Link className="font-medium text-sky-700 hover:underline" href="/login">Sign in</Link>
            </>
          )}
        </p>
      </div>
    </main>
  );
}
