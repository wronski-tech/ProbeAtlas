"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { Button, ErrorBanner } from "@/components/ui";

/**
 * Accept a workspace invitation. Joining is an explicit click by the signed-in invitee (never automatic
 * on page load), and only works for the account whose email the invitation was sent to.
 */
export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const [error, setError] = useState<ApiError | string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Accept the invitation, switch to the joined workspace, and open its dashboard. */
  async function accept() {
    setBusy(true);
    setError(null);
    try {
      const ws = await api<{ id: string }>("/v1/invitations/accept", { method: "POST", body: { token } });
      await fetch("/api/session/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-probeatlas-csrf": "1" },
        body: JSON.stringify({ workspace_id: ws.id }),
      });
      router.push("/dashboard");
      router.refresh();
    } catch (e) {
      // A signed-out visitor is sent to sign in by api() and returned here afterwards.
      const err = e as ApiError;
      if (err.status !== 401) setError(err.status === 404 ? "This invitation is invalid, expired, or was sent to a different email address." : err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 text-center">
        <div className="text-lg font-semibold">ProbeAtlas</div>
        <p className="mt-2 text-sm text-slate-600">You have been invited to join a workspace.</p>
        <p className="mt-1 text-sm text-slate-600">Sign in with the invited email address, then accept.</p>
        <Button className="mt-4 w-full" disabled={busy} onClick={accept}>{busy ? "Joining…" : "Accept invitation"}</Button>
        <div className="mt-3 text-left"><ErrorBanner error={error} /></div>
      </div>
    </main>
  );
}
