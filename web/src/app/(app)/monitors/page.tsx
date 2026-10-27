"use client";

import Link from "next/link";
import { useState } from "react";
import { useApi } from "@/lib/api";
import { describeTarget, formatInterval, query, relativeTime } from "@/lib/format";
import type { Monitor, Page } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorBanner, PageHeader, Select, Table, Td } from "@/components/ui";

export default function MonitorsPage() {
  const [state, setState] = useState("");
  const [type, setType] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<string[]>([]);
  const { data, error } = useApi<Page<Monitor>>(`/v1/monitors${query({ state, type, cursor, limit: 50 })}`, 15_000);

  return (
    <>
      <PageHeader
        title="Monitors"
        subtitle="Targets measured by your selected monitoring regions."
        actions={
          <Link href="/monitors/new">
            <Button>New monitor</Button>
          </Link>
        }
      />
      <Card>
        <div className="mb-3 flex flex-wrap gap-2">
          <Select aria-label="State" value={state} onChange={(e) => { setState(e.target.value); setCursor(null); setHistory([]); }} className="w-40">
            <option value="">All states</option>
            {["up", "degraded", "down", "unknown", "paused"].map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </Select>
          <Select aria-label="Type" value={type} onChange={(e) => { setType(e.target.value); setCursor(null); setHistory([]); }} className="w-40">
            <option value="">All types</option>
            {["http", "dns", "tcp", "icmp"].map((s) => (
              <option key={s} value={s}>{s.toUpperCase()}</option>
            ))}
          </Select>
        </div>
        <ErrorBanner error={error} />
        <Table head={["Name", "State", "Type", "Target", "Interval", "Regions", "Last result"]} empty={data?.items.length === 0 && <Empty>No monitors match.</Empty>}>
          {data?.items.map((m) => (
            <tr key={m.id} className="hover:bg-slate-50">
              <Td>
                <Link href={`/monitors/${m.id}`} className="font-medium text-sky-700 hover:underline">{m.name}</Link>
              </Td>
              <Td><Badge value={m.state} /></Td>
              <Td className="uppercase text-xs font-medium text-slate-500">{m.type}</Td>
              <Td className="max-w-xs truncate font-mono text-xs" title={describeTarget(m)}>{describeTarget(m)}</Td>
              <Td>{formatInterval(m.interval_seconds)}</Td>
              <Td className="text-xs">{m.regions.join(", ")}</Td>
              <Td className="text-xs text-slate-500">{relativeTime(m.last_result_at)}</Td>
            </tr>
          ))}
        </Table>
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="secondary" disabled={history.length === 0} onClick={() => { const h = [...history]; const prev = h.pop() ?? null; setHistory(h); setCursor(prev); }}>Previous</Button>
          <Button variant="secondary" disabled={!data?.next_cursor} onClick={() => { setHistory([...history, cursor ?? ""]); setCursor(data!.next_cursor); }}>Next</Button>
        </div>
      </Card>
    </>
  );
}
