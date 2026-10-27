"use client";

import { useEffect, useMemo, useState } from "react";
import { api, useApi } from "@/lib/api";
import { query, rangeToSince, type TimeRangeKey } from "@/lib/format";
import type { Agent, Location, Measurement, Monitor, Page } from "@/lib/types";
import { Button, Card, ErrorBanner, PageHeader, RangePicker, Select } from "@/components/ui";
import { MeasurementsTable } from "@/components/tables";

export default function MeasurementsPage() {
  const [range, setRange] = useState<TimeRangeKey>("24h");
  const [filters, setFilters] = useState({ monitor_id: "", region: "", agent_id: "", check_type: "", success: "" });
  const [rows, setRows] = useState<Measurement[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);
  const { data: monitors } = useApi<Page<Monitor>>("/v1/monitors?limit=200");
  const { data: agents } = useApi<Agent[]>("/v1/agents");
  const { data: locations } = useApi<Location[]>("/v1/locations");
  const monitorMap = useMemo(() => Object.fromEntries((monitors?.items ?? []).map((m) => [m.id, m])), [monitors]);
  const agentMap = useMemo(() => Object.fromEntries((agents ?? []).map((a) => [a.id, a.name])), [agents]);

  async function load(next: string | null) {
    setLoading(true);
    setError(null);
    try {
      const page = await api<Page<Measurement>>(`/v1/measurements${query({ ...filters, since: rangeToSince(range), limit: 100, cursor: next })}`);
      setRows((prev) => (next ? [...prev, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, filters]);

  const set = (k: keyof typeof filters) => (e: React.ChangeEvent<HTMLSelectElement>) => setFilters({ ...filters, [k]: e.target.value });

  return (
    <>
      <PageHeader title="Measurements" subtitle="Raw results reported by agents, newest first." actions={<RangePicker value={range} onChange={setRange} />} />
      <Card>
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
          <Select aria-label="Monitor" value={filters.monitor_id} onChange={set("monitor_id")}>
            <option value="">All monitors</option>
            {monitors?.items.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </Select>
          <Select aria-label="Region" value={filters.region} onChange={set("region")}>
            <option value="">All regions</option>
            {locations?.map((l) => <option key={l.region} value={l.region}>{l.region}</option>)}
          </Select>
          <Select aria-label="Agent" value={filters.agent_id} onChange={set("agent_id")}>
            <option value="">All agents</option>
            {agents?.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.region})</option>)}
          </Select>
          <Select aria-label="Measurement type" value={filters.check_type} onChange={set("check_type")}>
            <option value="">All types</option>
            {["http", "dns", "tcp", "icmp"].map((t) => <option key={t} value={t}>{t.toUpperCase()}</option>)}
          </Select>
          <Select aria-label="Result" value={filters.success} onChange={set("success")}>
            <option value="">All results</option>
            <option value="true">Success</option>
            <option value="false">Failed</option>
          </Select>
        </div>
        <ErrorBanner error={error} />
        <MeasurementsTable items={rows} monitors={monitorMap} agents={agentMap} />
        <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
          <span>{rows.length} shown</span>
          <Button variant="secondary" disabled={!cursor || loading} onClick={() => load(cursor)}>
            {loading ? "Loading…" : cursor ? "Load more" : "End of results"}
          </Button>
        </div>
      </Card>
    </>
  );
}
