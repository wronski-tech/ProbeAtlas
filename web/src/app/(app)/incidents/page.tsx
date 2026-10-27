"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { api, useApi } from "@/lib/api";
import { formatDateTime, query, relativeTime } from "@/lib/format";
import type { Incident, IncidentEvent, Monitor, Page } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorBanner, PageHeader, Select } from "@/components/ui";

export default function IncidentsPage() {
  const [status, setStatus] = useState("");
  const { data, error } = useApi<Page<Incident>>(`/v1/incidents${query({ status, limit: 100 })}`, 10_000);
  const { data: monitors } = useApi<Page<Monitor>>("/v1/monitors?limit=200");
  const names = useMemo(() => Object.fromEntries((monitors?.items ?? []).map((m) => [m.id, m.name])), [monitors]);
  const [open, setOpen] = useState<Record<string, IncidentEvent[]>>({});

  async function toggle(id: string) {
    if (open[id]) {
      const { [id]: _drop, ...rest } = open;
      return setOpen(rest);
    }
    const detail = await api<{ events: IncidentEvent[] }>(`/v1/incidents/${id}`);
    setOpen({ ...open, [id]: detail.events });
  }

  return (
    <>
      <PageHeader title="Incidents" subtitle="Opened by alert rules after consecutive failures; resolved after consecutive recoveries." />
      <Card
        actions={
          <Select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)} className="w-36 text-xs">
            <option value="">All</option>
            <option value="open">Open</option>
            <option value="resolved">Resolved</option>
          </Select>
        }
        title="History"
      >
        <ErrorBanner error={error} />
        {data?.items.length === 0 && <Empty>No incidents.</Empty>}
        <ul className="divide-y divide-slate-100">
          {data?.items.map((i) => (
            <li key={i.id} className="py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <Badge value={i.status} />
                    <span className="font-medium text-slate-900">{i.summary}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    <Link href={`/monitors/${i.monitor_id}`} className="text-sky-700 hover:underline">{names[i.monitor_id] ?? "monitor"}</Link>
                    {" · "}{i.kind.replace("_", " ")} · scope <span className="font-medium">{i.scope.replace("_", " ")}</span> · regions {i.affected_regions.join(", ")}
                    {" · "}opened {formatDateTime(i.opened_at)}
                    {i.resolved_at && <> · resolved after {relativeTime(i.opened_at, new Date(i.resolved_at)).replace(" ago", "")}</>}
                  </div>
                </div>
                <Button variant="ghost" aria-expanded={Boolean(open[i.id])} onClick={() => toggle(i.id)}>{open[i.id] ? "Hide events" : "Show events"}</Button>
              </div>
              {open[i.id] && (
                <ol className="mt-2 space-y-1 border-l-2 border-slate-200 pl-3 text-sm">
                  {open[i.id].map((e) => (
                    <li key={e.id}><span className="text-xs text-slate-500">{formatDateTime(e.created_at)}</span> <span className="font-medium">{e.kind}</span> — {e.message}</li>
                  ))}
                </ol>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}
