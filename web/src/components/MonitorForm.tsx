"use client";

import { useState } from "react";
import { ApiError, useApi } from "@/lib/api";
import type { CheckType, Location, Monitor } from "@/lib/types";
import { Button, ErrorBanner, Field, Input, Select } from "./ui";

export type MonitorFormValues = {
  name: string;
  type: CheckType;
  target: string;
  interval_seconds: number;
  timeout_ms: number;
  regions: string[];
  latency_threshold_ms: number | null;
  settings: Record<string, unknown>;
};

const PLACEHOLDER: Record<CheckType, string> = {
  http: "https://api.example.com/health",
  dns: "example.com",
  tcp: "db.example.com",
  icmp: "example.com",
};

/** Create or edit a monitor. Type-specific settings fields change with the selected check type. */
export function MonitorForm({ initial, editing, onSubmit, submitLabel, extra }: {
  initial?: Monitor;
  editing?: boolean;
  onSubmit: (v: MonitorFormValues) => Promise<void>;
  submitLabel: string;
  extra?: React.ReactNode;
}) {
  const { data: locations } = useApi<Location[]>("/v1/locations");
  const [type, setType] = useState<CheckType>(initial?.type ?? "http");
  const [regions, setRegions] = useState<string[]>(initial?.regions ?? []);
  const [error, setError] = useState<ApiError | string | null>(null);
  const [busy, setBusy] = useState(false);
  const s = initial?.settings ?? {};

  /** Build the request body from the form and submit it; validation errors are shown inline. */
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const str = (k: string) => String(f.get(k) ?? "").trim();
    if (regions.length === 0) return setError("Select at least one monitoring region.");
    const settings: Record<string, unknown> = {};
    if (type === "http") {
      settings.method = str("method") || "GET";
      settings.expected_status_codes = str("expected").split(",").map((x) => x.trim()).filter(Boolean);
      settings.follow_redirects = f.get("follow_redirects") === "on";
    } else if (type === "tcp") {
      settings.port = Number(str("port"));
    } else if (type === "dns") {
      settings.record_type = str("record_type") || "A";
      if (str("nameserver")) settings.nameserver = str("nameserver");
    } else if (type === "icmp") {
      settings.packet_count = Number(str("packet_count") || 3);
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit({
        name: str("name"),
        type,
        target: str("target"),
        interval_seconds: Number(str("interval_seconds")),
        timeout_ms: Number(str("timeout_ms")),
        regions,
        latency_threshold_ms: str("latency_threshold_ms") ? Number(str("latency_threshold_ms")) : null,
        settings,
      });
    } catch (err) {
      setError(err instanceof ApiError ? err : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="grid gap-4 md:grid-cols-2">
        <Field label="Name">
          <Input name="name" required maxLength={100} defaultValue={initial?.name} placeholder="Public API" />
        </Field>
        <Field label="Check type" hint={editing ? "The type cannot be changed after creation." : undefined}>
          <Select value={type} disabled={editing} onChange={(e) => setType(e.target.value as CheckType)}>
            <option value="http">HTTP / HTTPS</option>
            <option value="dns">DNS resolution</option>
            <option value="tcp">TCP connect</option>
            <option value="icmp">ICMP ping</option>
          </Select>
        </Field>
        <Field label={type === "http" ? "URL" : "Hostname or IP"} hint="Public internet destinations only. Private and cloud-metadata addresses are rejected.">
          <Input name="target" required defaultValue={initial?.target} placeholder={PLACEHOLDER[type]} className="font-mono" />
        </Field>
        {type === "http" && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Method">
              <Select name="method" defaultValue={String(s.method ?? "GET")}>
                <option>GET</option>
                <option>HEAD</option>
              </Select>
            </Field>
            <Field label="Expected status" hint="e.g. 2xx, 200-299, 301">
              <Input name="expected" defaultValue={((s.expected_status_codes as string[]) ?? ["2xx"]).join(", ")} />
            </Field>
            <label className="col-span-2 flex items-center gap-2 text-sm text-slate-700">
              <input type="checkbox" name="follow_redirects" defaultChecked={s.follow_redirects !== false} /> Follow redirects (each hop is re-validated)
            </label>
          </div>
        )}
        {type === "tcp" && (
          <Field label="Port">
            <Input name="port" type="number" min={1} max={65535} required defaultValue={String(s.port ?? "")} />
          </Field>
        )}
        {type === "dns" && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Record type">
              <Select name="record_type" defaultValue={String(s.record_type ?? "A")}>
                {["A", "AAAA", "CNAME", "MX", "TXT", "NS"].map((r) => <option key={r}>{r}</option>)}
              </Select>
            </Field>
            <Field label="Nameserver (optional)" hint="Public resolver IP; default is the agent's resolver.">
              <Input name="nameserver" defaultValue={String(s.nameserver ?? "")} placeholder="1.1.1.1" />
            </Field>
          </div>
        )}
        {type === "icmp" && (
          <Field label="Packets per check" hint="Agents without ICMP support report checks as unsupported.">
            <Input name="packet_count" type="number" min={1} max={10} defaultValue={String(s.packet_count ?? 3)} />
          </Field>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Field label="Interval (seconds)">
          <Input name="interval_seconds" type="number" min={10} max={86400} required defaultValue={initial?.interval_seconds ?? 60} />
        </Field>
        <Field label="Timeout (ms)">
          <Input name="timeout_ms" type="number" min={500} max={30000} required defaultValue={initial?.timeout_ms ?? 10000} />
        </Field>
        <Field label="Latency threshold (ms, optional)" hint="Slower successful checks mark the monitor degraded.">
          <Input name="latency_threshold_ms" type="number" min={1} defaultValue={initial?.latency_threshold_ms ?? ""} />
        </Field>
      </div>

      <fieldset>
        <legend className="mb-1 text-sm font-medium text-slate-700">Monitoring regions</legend>
        <p className="mb-2 text-xs text-slate-500">Regions are operator-assigned agent locations. Each check runs once per selected region.</p>
        {locations?.length === 0 && <ErrorBanner error="No agents are enrolled yet, so no regions are available." />}
        <div className="flex flex-wrap gap-2">
          {locations?.map((l) => {
            const on = regions.includes(l.region);
            const supported = l.capabilities.length === 0 || l.capabilities.includes(type);
            return (
              <label key={l.region} className={`flex cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-sm ${on ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 bg-white text-slate-700"}`}>
                <input type="checkbox" className="sr-only" checked={on} onChange={() => setRegions(on ? regions.filter((r) => r !== l.region) : [...regions, l.region])} />
                <span className="font-medium">{l.region}</span>
                <span className={`text-xs ${on ? "text-slate-300" : "text-slate-500"}`}>
                  {[l.cities.join("/"), l.country_codes.join("/")].filter(Boolean).join(", ") || "location not set"} · {l.agents_online}/{l.agents_total} online
                  {!supported && " · no agent supports this check"}
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      {extra}
      <ErrorBanner error={error} />
      <Button type="submit" disabled={busy}>{busy ? "Saving…" : submitLabel}</Button>
    </form>
  );
}
