"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useApi } from "@/lib/api";
import { formatMs, formatPct, query, relativeTime } from "@/lib/format";
import type { Agent, Monitor, Overview, Page } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorBanner, PageHeader, Stat, Table, Td } from "@/components/ui";
import { MeasurementsTable, RegionTable } from "@/components/tables";

const WINDOWS = [1, 24, 168];

/** Main dashboard: monitor states, failing monitors, incidents, availability, latency, agents, regions. */
export default function Dashboard() {
  const [hours, setHours] = useState(24);
  const { data, error } = useApi<Overview>(`/v1/overview${query({ window_hours: hours })}`, 10_000);
  const { data: monitors } = useApi<Page<Monitor>>("/v1/monitors?limit=200", 30_000);
  const { data: agents } = useApi<Agent[]>("/v1/agents", 15_000);
  const monitorMap = useMemo(() => Object.fromEntries((monitors?.items ?? []).map((m) => [m.id, m])), [monitors]);
  const agentMap = useMemo(() => Object.fromEntries((agents ?? []).map((a) => [a.id, a.name])), [agents]);

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Live status of your monitors, refreshed every 10 seconds."
        actions={
          <div role="group" aria-label="Window" className="inline-flex rounded-md border border-slate-300 bg-white p-0.5">
            {WINDOWS.map((h) => (
              <button
                key={h}
                aria-pressed={hours === h}
                onClick={() => setHours(h)}
                className={`rounded px-2.5 py-1 text-xs font-medium ${hours === h ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`}
              >
                {h === 168 ? "7d" : `${h}h`}
              </button>
            ))}
          </div>
        }
      />
      <ErrorBanner error={error} />
      {data && data.monitors.total === 0 ? (
        <Card>
          <div className="py-8 text-center">
            <p className="text-sm text-slate-600">No monitors yet. Create one to start collecting measurements from your agents.</p>
            <Link href="/monitors/new">
              <Button className="mt-4">Create monitor</Button>
            </Link>
          </div>
        </Card>
      ) : (
        data && (
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
              <Stat label="Up" value={data.monitors.up} tone="good" sub={`of ${data.monitors.total} monitors`} />
              <Stat label="Degraded" value={data.monitors.degraded} tone={data.monitors.degraded ? "warn" : undefined} />
              <Stat label="Down" value={data.monitors.down} tone={data.monitors.down ? "bad" : undefined} sub={`${data.monitors.unknown} unknown · ${data.monitors.paused} paused`} />
              <Stat label={`Availability (${hours === 168 ? "7d" : `${hours}h`})`} value={formatPct(data.availability_pct)} sub="successful checks / all checks" />
              <Stat label="HTTP latency P50" value={formatMs(data.latency.p50_ms)} sub={`P95 ${formatMs(data.latency.p95_ms)} · ${data.latency.sample_count} samples`} />
              <Stat label="Agents online" value={`${data.agents.online ?? 0}/${(data.agents.online ?? 0) + (data.agents.offline ?? 0)}`} tone={data.agents.offline ? "warn" : "good"} sub={`${data.open_incidents} open incident(s)`} />
            </div>

            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Failing monitors">
                {data.failing_monitors.length === 0 ? (
                  <Empty>All active monitors are passing.</Empty>
                ) : (
                  <ul className="divide-y divide-slate-100">
                    {data.failing_monitors.map((m) => (
                      <li key={m.id} className="flex items-center justify-between py-2">
                        <Link href={`/monitors/${m.id}`} className="font-medium text-sky-700 hover:underline">
                          {m.name}
                        </Link>
                        <span className="flex items-center gap-2 text-xs text-slate-500">
                          since {relativeTime(m.state_changed_at)} <Badge value={m.state} />
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
              <Card title="Recent incidents" actions={<Link href="/incidents" className="text-xs text-sky-700 hover:underline">All incidents</Link>}>
                {data.recent_incidents.length === 0 ? (
                  <Empty>No incidents.</Empty>
                ) : (
                  <ul className="divide-y divide-slate-100">
                    {data.recent_incidents.slice(0, 6).map((i) => (
                      <li key={i.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                        <Link href={`/monitors/${i.monitor_id}`} className="truncate text-slate-800 hover:underline">
                          {i.summary}
                        </Link>
                        <span className="flex shrink-0 items-center gap-2 text-xs text-slate-500">
                          {relativeTime(i.opened_at)} <Badge value={i.status} />
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>

            <div className="grid gap-5 lg:grid-cols-3">
              <Card title="Performance by region" className="lg:col-span-2">
                <p className="mb-2 text-xs text-slate-500">Success rate across all checks; latency percentiles from HTTP checks only. Percentiles are hidden until enough samples exist.</p>
                <RegionTable groups={data.regions} showPercentiles={false} />
              </Card>
              <Card title="Agents" actions={<Link href="/agents" className="text-xs text-sky-700 hover:underline">Details</Link>}>
                <Table head={["Agent", "Region", "Status"]} empty={agents?.length === 0 && <Empty>No agents enrolled.</Empty>}>
                  {(agents ?? []).map((a) => (
                    <tr key={a.id}>
                      <Td className="font-medium">{a.name}</Td>
                      <Td>{a.region}</Td>
                      <Td title={`last heartbeat ${relativeTime(a.last_heartbeat_at)}`}>
                        <Badge value={a.status} />
                      </Td>
                    </tr>
                  ))}
                </Table>
              </Card>
            </div>

            <Card title="Most recent measurements" actions={<Link href="/measurements" className="text-xs text-sky-700 hover:underline">Explore</Link>}>
              <MeasurementsTable items={data.recent_measurements} monitors={monitorMap} agents={agentMap} />
            </Card>
          </div>
        )
      )}
    </>
  );
}
