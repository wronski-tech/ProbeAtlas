"use client";

import Link from "next/link";
import type { GroupPerformance, Measurement, Monitor } from "@/lib/types";
import { formatDateTime, formatMs, formatPct, relativeTime } from "@/lib/format";
import { Badge, Empty, ResultBadge, Table, Td, cx } from "./ui";

export function MeasurementsTable({ items, monitors, agents, showMonitor = true }: {
  items: Measurement[];
  monitors?: Record<string, Monitor>;
  agents?: Record<string, string>;
  showMonitor?: boolean;
}) {
  return (
    <Table
      head={["Measured", ...(showMonitor ? ["Monitor"] : []), "Region / agent", "Result", "Latency", "HTTP", "DNS", "Connect", "TLS", "TTFB", "Detail"]}
      empty={items.length === 0 && <Empty>No measurements in this range.</Empty>}
    >
      {items.map((m) => (
        <tr key={m.id} className="hover:bg-slate-50">
          <Td title={`measured ${m.measured_at}\ningested ${m.ingested_at}`}>
            <span className="tabular">{formatDateTime(m.measured_at)}</span>
            {m.late && <span className="ml-1 text-xs text-amber-700">(late)</span>}
          </Td>
          {showMonitor && (
            <Td>
              <Link href={`/monitors/${m.monitor_id}`} className="font-medium text-sky-700 hover:underline">
                {monitors?.[m.monitor_id]?.name ?? m.monitor_id.slice(0, 8)}
              </Link>
            </Td>
          )}
          <Td>
            <span className="font-medium">{m.region}</span>
            <span className="ml-1 text-xs text-slate-500">{m.agent_id ? agents?.[m.agent_id] ?? m.agent_id.slice(0, 8) : ""}</span>
          </Td>
          <Td>
            <ResultBadge success={m.success} category={m.failure_category} />
          </Td>
          <Td className="tabular font-medium">{formatMs(m.latency_ms)}</Td>
          <Td className="tabular">{m.http_status ?? "—"}</Td>
          <Td className="tabular">{formatMs(m.dns_ms)}</Td>
          <Td className="tabular">{formatMs(m.connect_ms)}</Td>
          <Td className="tabular">{formatMs(m.tls_ms)}</Td>
          <Td className="tabular">{formatMs(m.ttfb_ms)}</Td>
          <Td className="max-w-xs truncate text-xs text-slate-500" title={m.error_message ?? undefined}>
            {m.error_message ??
              (m.packet_loss_pct !== null ? `loss ${formatPct(m.packet_loss_pct, { digits: 0 })}` : m.remote_address ?? "")}
          </Td>
        </tr>
      ))}
    </Table>
  );
}

export function RegionTable({ groups, slow = [], showAgent = false, showPercentiles = true }: {
  groups: GroupPerformance[];
  slow?: string[];
  showAgent?: boolean;
  showPercentiles?: boolean;
}) {
  return (
    <Table
      head={[
        showAgent ? "Agent" : "Region",
        "Samples",
        "Success",
        "Missing",
        "Min",
        "P50",
        "P95",
        ...(showPercentiles ? ["P99"] : []),
        "Loss",
        "Last",
      ]}
      empty={groups.length === 0 && <Empty>No data for this window.</Empty>}
    >
      {groups.map((g) => {
        const rate = g.success_rate;
        return (
          <tr key={g.key} className={cx(slow.includes(g.region) && !showAgent && "bg-amber-50/60")}>
            <Td className="font-medium text-slate-900">
              {showAgent ? (
                <>
                  {g.agent_name ?? g.agent_id?.slice(0, 8)} <span className="text-xs text-slate-500">{g.region}</span>
                </>
              ) : (
                g.region
              )}
              {slow.includes(g.region) && !showAgent && <span className="ml-2 text-xs font-normal text-amber-800">slow</span>}
            </Td>
            <Td className="tabular">{g.total}</Td>
            <Td className={cx("tabular font-medium", rate === null ? "text-slate-400" : rate >= 0.99 ? "text-emerald-700" : rate >= 0.95 ? "text-amber-700" : "text-red-700")}>
              {formatPct(rate, { ratio: true })}
            </Td>
            <Td className={cx("tabular", g.missing > 0 && "text-amber-700")} title="Scheduled checks that produced no result (not counted as success)">
              {g.missing}
            </Td>
            <Td className="tabular">{formatMs(g.latency.min_ms)}</Td>
            <Td className="tabular" title={g.latency.p50_ms === null ? "Needs at least 5 successful samples" : undefined}>
              {formatMs(g.latency.p50_ms)}
            </Td>
            <Td className="tabular" title={g.latency.p95_ms === null ? "Needs at least 20 successful samples" : undefined}>
              {formatMs(g.latency.p95_ms)}
            </Td>
            {showPercentiles && (
              <Td className="tabular" title={g.latency.p99_ms === null ? "Needs at least 100 successful samples" : undefined}>
                {formatMs(g.latency.p99_ms)}
              </Td>
            )}
            <Td className="tabular">{formatPct(g.packet_loss_avg_pct, { digits: 1 })}</Td>
            <Td className="text-xs text-slate-500">{relativeTime(g.last_measured_at)}</Td>
          </tr>
        );
      })}
    </Table>
  );
}

export function StateBadge({ monitor }: { monitor: Pick<Monitor, "state"> }) {
  return <Badge value={monitor.state} />;
}
