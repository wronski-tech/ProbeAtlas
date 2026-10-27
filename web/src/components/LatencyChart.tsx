"use client";

import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Timeseries } from "@/lib/types";
import { formatMs } from "@/lib/format";
import { Empty } from "./ui";

const COLORS = ["#0f172a", "#0284c7", "#d97706", "#059669", "#7c3aed", "#dc2626", "#64748b"];

/** Average latency of successful checks per bucket and series. Gaps mean no successful sample. */
export function LatencyChart({ data, labels = {} }: { data: Timeseries; labels?: Record<string, string> }) {
  if (data.series.length === 0) return <Empty>No measurements in this window.</Empty>;
  const rows = new Map<string, Record<string, number | string | null>>();
  for (const s of data.series) {
    for (const p of s.points) {
      const row = rows.get(p.t) ?? { t: p.t };
      row[s.key] = p.avg_ms;
      rows.set(p.t, row);
    }
  }
  const points = [...rows.values()].sort((a, b) => String(a.t).localeCompare(String(b.t)));
  const short = data.bucket_seconds < 86400;
  const fmt = (t: string) =>
    new Date(t).toLocaleString(undefined, short ? { hour: "2-digit", minute: "2-digit" } : { month: "short", day: "numeric" });

  return (
    <div className="h-64 w-full" role="img" aria-label="Average latency over time per series">
      <ResponsiveContainer>
        <LineChart data={points} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#e2e8f0" strokeDasharray="3 3" />
          <XAxis dataKey="t" tickFormatter={fmt} tick={{ fontSize: 11, fill: "#64748b" }} minTickGap={32} />
          <YAxis tickFormatter={(v) => formatMs(v)} tick={{ fontSize: 11, fill: "#64748b" }} width={64} />
          <Tooltip
            labelFormatter={(t) => new Date(String(t)).toLocaleString()}
            formatter={(v, name) => [formatMs(typeof v === "number" ? v : null), labels[String(name)] ?? name]}
          />
          <Legend formatter={(v) => labels[String(v)] ?? v} wrapperStyle={{ fontSize: 12 }} />
          {data.series.map((s, i) => (
            <Line key={s.key} type="monotone" dataKey={s.key} stroke={COLORS[i % COLORS.length]} dot={false} strokeWidth={2} connectNulls={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
