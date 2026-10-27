"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ApiError, api, useApi } from "@/lib/api";
import { ASSESSMENT_TEXT, describeTarget, formatDateTime, formatInterval, formatMs, query, rangeToSince, relativeTime, type TimeRangeKey } from "@/lib/format";
import type { Agent, AlertRule, Measurement, Monitor, MonitorEvent, Page, RegionalPerformance, Timeseries } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorBanner, Field, Input, PageHeader, RangePicker, Select, Table, Td } from "@/components/ui";
import { LatencyChart } from "@/components/LatencyChart";
import { MeasurementsTable, RegionTable } from "@/components/tables";
import { MonitorForm } from "@/components/MonitorForm";

export default function MonitorDetail() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [range, setRange] = useState<TimeRangeKey>("1h");
  const [groupBy, setGroupBy] = useState<"region" | "agent">("region");
  const [regionFilter, setRegionFilter] = useState("");
  const [resultFilter, setResultFilter] = useState("");
  const [editing, setEditing] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  // Recompute the window when the range changes or on each refresh tick.
  const [tick, setTick] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const since = useMemo(() => rangeToSince(range), [range, tick]);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 60_000); // slide the window every minute
    return () => clearInterval(t);
  }, []);

  const monitor = useApi<Monitor>(`/v1/monitors/${id}`, 10_000);
  const perf = useApi<RegionalPerformance>(`/v1/monitors/${id}/regional-performance${query({ since })}`, 15_000);
  const series = useApi<Timeseries>(`/v1/monitors/${id}/timeseries${query({ since, group_by: groupBy })}`, 15_000);
  const results = useApi<Page<Measurement>>(
    `/v1/measurements${query({ monitor_id: id, since, region: regionFilter, success: resultFilter, limit: 25 })}`,
    10_000,
  );
  const rules = useApi<AlertRule[]>(`/v1/alert-rules?monitor_id=${id}`, 15_000);
  const events = useApi<MonitorEvent[]>(`/v1/monitors/${id}/events?limit=20`, 15_000);
  const agents = useApi<Agent[]>("/v1/agents");
  const agentNames = useMemo(() => Object.fromEntries((agents.data ?? []).map((a) => [a.id, a.name])), [agents.data]);

  const m = monitor.data;
  if (monitor.error?.status === 404) return <ErrorBanner error="Monitor not found." />;
  if (!m) return <ErrorBanner error={monitor.error} />;

  async function act(fn: () => Promise<unknown>) {
    setActionError(null);
    try {
      await fn();
      monitor.reload();
      rules.reload();
      events.reload();
    } catch (e) {
      setActionError(e as ApiError);
    }
  }

  const p = perf.data;
  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-3">{m.name} <Badge value={m.state} /></span>}
        subtitle={
          <span className="font-mono text-xs">
            {m.type.toUpperCase()} · {describeTarget(m)} · every {formatInterval(m.interval_seconds)} · timeout {formatMs(m.timeout_ms)}
          </span>
        }
        actions={
          <>
            <RangePicker value={range} onChange={(r) => { setRange(r); setTick((t) => t + 1); }} />
            <Button variant="secondary" onClick={() => act(() => api(`/v1/monitors/${id}/${m.paused ? "resume" : "pause"}`, { method: "POST" }))}>
              {m.paused ? "Resume" : "Pause"}
            </Button>
            <Button variant="secondary" onClick={() => setEditing(!editing)}>{editing ? "Close editor" : "Edit"}</Button>
            <Button
              variant="danger"
              onClick={() => confirm(`Delete monitor "${m.name}" and all of its measurements?`) && act(async () => { await api(`/v1/monitors/${id}`, { method: "DELETE" }); router.push("/monitors"); })}
            >
              Delete
            </Button>
          </>
        }
      />
      <ErrorBanner error={actionError} />

      {editing && (
        <Card title="Edit monitor" className="mb-5">
          <MonitorForm
            initial={m}
            editing
            submitLabel="Save changes"
            onSubmit={async (v) => {
              const { type: _t, ...body } = v;
              await api(`/v1/monitors/${id}`, { method: "PATCH", body });
              setEditing(false);
              monitor.reload();
            }}
          />
        </Card>
      )}

      <div className="space-y-5">
        <div className="grid gap-5 lg:grid-cols-3">
          <Card title="Target" className="lg:col-span-1">
            <dl className="grid grid-cols-3 gap-y-2 text-sm">
              <dt className="text-slate-500">State</dt><dd className="col-span-2"><Badge value={m.state} /> <span className="text-xs text-slate-500">since {relativeTime(m.state_changed_at)}</span></dd>
              <dt className="text-slate-500">Target</dt><dd className="col-span-2 min-w-0 break-all font-mono text-xs">{describeTarget(m)}</dd>
              <dt className="text-slate-500">Regions</dt><dd className="col-span-2">{m.regions.join(", ")}</dd>
              <dt className="text-slate-500">Last result</dt><dd className="col-span-2">{relativeTime(m.last_result_at)}</dd>
              <dt className="text-slate-500">Latency limit</dt><dd className="col-span-2">{formatMs(m.latency_threshold_ms)}</dd>
              {m.type === "http" && (<><dt className="text-slate-500">Expected</dt><dd className="col-span-2">{(m.settings.expected_status_codes as string[])?.join(", ")}</dd></>)}
              <dt className="text-slate-500">Config</dt><dd className="col-span-2 text-xs text-slate-500">v{m.config_version}, updated {relativeTime(m.updated_at)}</dd>
            </dl>
          </Card>
          <Card title="Regional comparison" className="lg:col-span-2" actions={p && <Badge value={p.assessment} label={ASSESSMENT_TEXT[p.assessment]} />}>
            <ErrorBanner error={perf.error} />
            {p && (
              <>
                <RegionTable groups={p.regions} slow={p.slow_regions} />
                {p.slow_regions.length > 0 && (
                  <p className="mt-2 text-xs text-amber-800">Highlighted regions have a median latency more than twice the median of the other regions.</p>
                )}
                {p.agents.length > p.regions.length && (
                  <details className="mt-3">
                    <summary className="cursor-pointer text-xs font-medium text-slate-600">Per-agent breakdown</summary>
                    <div className="mt-2"><RegionTable groups={p.agents} showAgent /></div>
                  </details>
                )}
              </>
            )}
          </Card>
        </div>

        <Card
          title="Latency over time"
          actions={
            <Select aria-label="Group by" value={groupBy} onChange={(e) => setGroupBy(e.target.value as "region" | "agent")} className="w-36 text-xs">
              <option value="region">By region</option>
              <option value="agent">By agent</option>
            </Select>
          }
        >
          <p className="mb-2 text-xs text-slate-500">Average latency of successful checks{series.data ? ` per ${formatInterval(series.data.bucket_seconds)} bucket` : ""}. Gaps mean no successful measurement.</p>
          {series.data && <LatencyChart data={series.data} labels={groupBy === "agent" ? agentNames : {}} />}
        </Card>

        <div className="grid gap-5 lg:grid-cols-3">
          <Card title="Failure categories">
            {p && Object.keys(p.failure_categories).length === 0 ? (
              <Empty>No failures in this window.</Empty>
            ) : (
              <ul className="space-y-1 text-sm">
                {p && Object.entries(p.failure_categories).map(([k, v]) => (
                  <li key={k} className="flex justify-between"><span className="font-mono text-xs">{k}</span><span className="tabular font-medium">{v}</span></li>
                ))}
              </ul>
            )}
          </Card>
          <AlertRules monitor={m} rules={rules.data ?? []} onChange={() => { rules.reload(); events.reload(); }} />
          <Card title="Recent events">
            {events.data?.length === 0 ? <Empty>No events yet.</Empty> : (
              <ol className="space-y-2 text-sm">
                {events.data?.map((e, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="w-20 shrink-0 text-xs text-slate-500" title={formatDateTime(e.at)}>{relativeTime(e.at)}</span>
                    <span className={e.source === "incident" ? "text-slate-900" : "text-slate-500"}>{e.message}</span>
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>

        <Card
          title="Measurements"
          actions={
            <div className="flex flex-wrap gap-2">
              <Select aria-label="Region" value={regionFilter} onChange={(e) => setRegionFilter(e.target.value)} className="w-36 text-xs">
                <option value="">All regions</option>
                {m.regions.map((r) => <option key={r} value={r}>{r}</option>)}
              </Select>
              <Select aria-label="Result" value={resultFilter} onChange={(e) => setResultFilter(e.target.value)} className="w-32 text-xs">
                <option value="">All results</option>
                <option value="true">Success</option>
                <option value="false">Failed</option>
              </Select>
            </div>
          }
        >
          <MeasurementsTable items={results.data?.items ?? []} agents={agentNames} showMonitor={false} />
        </Card>
      </div>
    </>
  );
}

function AlertRules({ monitor, rules, onChange }: { monitor: Monitor; rules: AlertRule[]; onChange: () => void }) {
  const [error, setError] = useState<ApiError | null>(null);
  const [kind, setKind] = useState("unavailable");
  const kinds = ["unavailable", ...(monitor.type === "http" ? ["http_status"] : []), "latency", ...(monitor.type === "icmp" ? ["packet_loss"] : [])];

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    setError(null);
    try {
      await api("/v1/alert-rules", {
        method: "POST",
        body: {
          monitor_id: monitor.id,
          kind,
          threshold: f.get("threshold") ? Number(f.get("threshold")) : null,
          consecutive_breaches: Number(f.get("breaches")),
          recovery_successes: Number(f.get("recovery")),
          min_regions: Number(f.get("min_regions")),
        },
      });
      form.reset();
      onChange();
    } catch (err) {
      setError(err as ApiError);
    }
  }

  return (
    <Card title="Alert rules">
      {rules.length === 0 ? <Empty>No alert rules.</Empty> : (
        <Table head={["Rule", "Trigger", ""]}>
          {rules.map((r) => (
            <tr key={r.id}>
              <Td>
                <div className="font-medium">{r.kind.replace("_", " ")}{r.threshold !== null && ` > ${r.kind === "latency" ? formatMs(r.threshold) : `${r.threshold}%`}`}</div>
                {r.firing_regions.length > 0 && <div className="text-xs text-red-700">firing: {r.firing_regions.join(", ")}</div>}
              </Td>
              <Td className="text-xs text-slate-500">{r.consecutive_breaches}× to fire, {r.recovery_successes}× to recover, ≥{r.min_regions} region(s)</Td>
              <Td>
                <Button variant="ghost" aria-label={`Delete ${r.kind} rule`} onClick={async () => { await api(`/v1/alert-rules/${r.id}`, { method: "DELETE" }); onChange(); }}>Remove</Button>
              </Td>
            </tr>
          ))}
        </Table>
      )}
      <form onSubmit={create} className="mt-4 grid grid-cols-2 gap-2 border-t border-slate-100 pt-3">
        <Field label="Kind">
          <Select value={kind} onChange={(e) => setKind(e.target.value)}>
            {kinds.map((k) => <option key={k} value={k}>{k.replace("_", " ")}</option>)}
          </Select>
        </Field>
        <Field label={kind === "packet_loss" ? "Threshold (%)" : "Threshold (ms)"}>
          <Input name="threshold" type="number" min={1} disabled={kind === "unavailable" || kind === "http_status"} required={kind === "latency" || kind === "packet_loss"} />
        </Field>
        <Field label="Breaches to fire"><Input name="breaches" type="number" min={1} max={20} defaultValue={3} /></Field>
        <Field label="Successes to recover"><Input name="recovery" type="number" min={1} max={20} defaultValue={2} /></Field>
        <Field label="Min. regions"><Input name="min_regions" type="number" min={1} max={monitor.regions.length} defaultValue={1} /></Field>
        <div className="flex items-end"><Button type="submit" variant="secondary" className="w-full">Add rule</Button></div>
        <div className="col-span-2"><ErrorBanner error={error} /></div>
      </form>
    </Card>
  );
}
