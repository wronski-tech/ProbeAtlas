"use client";

import { useState } from "react";
import { ApiError, api, useApi } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { ApiKey, Channel, Member } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorBanner, Field, Input, PageHeader, SecretOnce, Select, Table, Td } from "@/components/ui";

export default function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" subtitle="API access, notifications, and workspace members." />
      <div className="space-y-5">
        <ApiKeys />
        <Channels />
        <Members />
      </div>
    </>
  );
}

function useAction() {
  const [error, setError] = useState<ApiError | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e as ApiError);
    }
  };
  return { error, run };
}

function ApiKeys() {
  const keys = useApi<ApiKey[]>("/v1/api-keys");
  const [secret, setSecret] = useState<string | null>(null);
  const { error, run } = useAction();

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    await run(async () => {
      const k = await api<ApiKey>("/v1/api-keys", {
        method: "POST",
        body: { name: f.get("name"), expires_in_days: f.get("expires") ? Number(f.get("expires")) : null },
      });
      setSecret(k.key ?? null);
      form.reset();
      keys.reload();
    });
  }

  return (
    <Card title="API keys">
      <p className="mb-3 text-sm text-slate-600">
        Use API keys with the REST API and CLI: <code className="rounded bg-slate-100 px-1 text-xs">Authorization: Bearer pak_…</code>. Keys are stored hashed; only workspace owners can manage them.
      </p>
      {keys.error?.status === 403 ? <ErrorBanner error="Only workspace owners can manage API keys." /> : <ErrorBanner error={error ?? keys.error} />}
      {secret && <div className="mb-3"><SecretOnce label="New API key" secret={secret} onDone={() => setSecret(null)} /></div>}
      <Table head={["Name", "Key", "Created", "Last used", "Expires", "Status", ""]} empty={keys.data?.length === 0 && <Empty>No API keys.</Empty>}>
        {keys.data?.map((k) => (
          <tr key={k.id}>
            <Td className="font-medium">{k.name}</Td>
            <Td className="font-mono text-xs">{k.prefix}_…</Td>
            <Td className="text-xs">{formatDateTime(k.created_at)}</Td>
            <Td className="text-xs">{relativeTime(k.last_used_at)}</Td>
            <Td className="text-xs">{k.expires_at ? formatDateTime(k.expires_at) : "never"}</Td>
            <Td><Badge value={k.revoked_at ? "offline" : "online"} label={k.revoked_at ? "revoked" : "active"} /></Td>
            <Td>
              {!k.revoked_at && (
                <span className="flex gap-1">
                  <Button variant="ghost" onClick={() => run(async () => { const n = await api<ApiKey>(`/v1/api-keys/${k.id}/rotate`, { method: "POST" }); setSecret(n.key ?? null); keys.reload(); })}>Rotate</Button>
                  <Button variant="ghost" onClick={() => confirm(`Revoke "${k.name}"? Clients using it will stop working.`) && run(async () => { await api(`/v1/api-keys/${k.id}`, { method: "DELETE" }); keys.reload(); })}>Revoke</Button>
                </span>
              )}
            </Td>
          </tr>
        ))}
      </Table>
      <form onSubmit={create} className="mt-4 flex flex-wrap items-end gap-2 border-t border-slate-100 pt-3">
        <Field label="Name"><Input name="name" required maxLength={100} placeholder="ci-pipeline" /></Field>
        <Field label="Expires in (days)"><Input name="expires" type="number" min={1} max={3650} placeholder="never" className="w-32" /></Field>
        <Button type="submit">Create key</Button>
      </form>
    </Card>
  );
}

function Channels() {
  const channels = useApi<Channel[]>("/v1/notification-channels");
  const [secret, setSecret] = useState<string | null>(null);
  const [test, setTest] = useState<Record<string, string>>({});
  const { error, run } = useAction();

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    await run(async () => {
      const c = await api<Channel>("/v1/notification-channels", { method: "POST", body: { name: f.get("name"), url: f.get("url") } });
      setSecret(c.signing_secret ?? null);
      form.reset();
      channels.reload();
    });
  }

  return (
    <Card title="Webhook notifications">
      <p className="mb-3 text-sm text-slate-600">
        Incident opened/resolved events are POSTed as JSON to each enabled webhook. Verify the <code className="rounded bg-slate-100 px-1 text-xs">X-ProbeAtlas-Signature</code> header (HMAC-SHA256 of <code className="rounded bg-slate-100 px-1 text-xs">timestamp.body</code>).
      </p>
      <ErrorBanner error={error ?? channels.error} />
      {secret && <div className="mb-3"><SecretOnce label="Webhook signing secret" secret={secret} onDone={() => setSecret(null)} /></div>}
      <Table head={["Name", "URL", "Status", ""]} empty={channels.data?.length === 0 && <Empty>No webhooks configured.</Empty>}>
        {channels.data?.map((c) => (
          <tr key={c.id}>
            <Td className="font-medium">{c.name}</Td>
            <Td className="max-w-xs truncate font-mono text-xs" title={c.url}>{c.url}</Td>
            <Td><Badge value={c.enabled ? "online" : "offline"} label={c.enabled ? "enabled" : "disabled"} /> {test[c.id] && <span className="ml-2 text-xs text-slate-600">{test[c.id]}</span>}</Td>
            <Td>
              <span className="flex gap-1">
                <Button variant="ghost" onClick={() => run(async () => {
                  const r = await api<{ delivered: boolean; status_code: number | null; error: string | null }>(`/v1/notification-channels/${c.id}/test`, { method: "POST" });
                  setTest({ ...test, [c.id]: r.delivered ? `test delivered (HTTP ${r.status_code})` : `test failed: ${r.error}` });
                })}>Send test</Button>
                <Button variant="ghost" onClick={() => run(async () => { await api(`/v1/notification-channels/${c.id}`, { method: "PATCH", body: { enabled: !c.enabled } }); channels.reload(); })}>{c.enabled ? "Disable" : "Enable"}</Button>
                <Button variant="ghost" onClick={() => confirm(`Delete webhook "${c.name}"?`) && run(async () => { await api(`/v1/notification-channels/${c.id}`, { method: "DELETE" }); channels.reload(); })}>Delete</Button>
              </span>
            </Td>
          </tr>
        ))}
      </Table>
      <form onSubmit={create} className="mt-4 flex flex-wrap items-end gap-2 border-t border-slate-100 pt-3">
        <Field label="Name"><Input name="name" required maxLength={100} placeholder="on-call" /></Field>
        <Field label="URL"><Input name="url" required type="url" placeholder="https://hooks.example.com/probeatlas" className="w-80" /></Field>
        <Button type="submit">Add webhook</Button>
      </form>
    </Card>
  );
}

function Members() {
  const members = useApi<Member[]>("/v1/members");
  const { error, run } = useAction();

  async function add(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    await run(async () => {
      await api("/v1/members", { method: "POST", body: { email: f.get("email"), role: f.get("role") } });
      form.reset();
      members.reload();
    });
  }

  return (
    <Card title="Members">
      <ErrorBanner error={error ?? members.error} />
      <Table head={["Name", "Email", "Role", "Since", ""]}>
        {members.data?.map((m) => (
          <tr key={m.user_id}>
            <Td className="font-medium">{m.name}</Td>
            <Td>{m.email}</Td>
            <Td>{m.role}</Td>
            <Td className="text-xs">{formatDateTime(m.created_at)}</Td>
            <Td><Button variant="ghost" onClick={() => confirm(`Remove ${m.email}?`) && run(async () => { await api(`/v1/members/${m.user_id}`, { method: "DELETE" }); members.reload(); })}>Remove</Button></Td>
          </tr>
        ))}
      </Table>
      <form onSubmit={add} className="mt-4 flex flex-wrap items-end gap-2 border-t border-slate-100 pt-3">
        <Field label="Email of a registered user"><Input name="email" type="email" required className="w-72" /></Field>
        <Field label="Role"><Select name="role" defaultValue="member"><option value="member">member</option><option value="owner">owner</option></Select></Field>
        <Button type="submit">Add member</Button>
      </form>
    </Card>
  );
}
