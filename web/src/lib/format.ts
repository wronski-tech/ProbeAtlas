// Pure formatting helpers (unit tested in format.test.ts).

/** Milliseconds for display. Missing values render as an em dash, never as 0. */
export function formatMs(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (v >= 10_000) return `${(v / 1000).toFixed(1)} s`;
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`;
  if (v >= 100) return `${Math.round(v)} ms`;
  return `${v.toFixed(1)} ms`;
}

/** Ratio (0-1) or percentage (0-100) for display. */
export function formatPct(v: number | null | undefined, { ratio = false, digits = 2 } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const pct = ratio ? v * 100 : v;
  if (pct === 100) return "100%";
  return `${pct.toFixed(digits)}%`;
}

/** Relative time such as `5m ago` or `in 2h`; `never` when the timestamp is missing. */
export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "never";
  const diff = (now.getTime() - new Date(iso).getTime()) / 1000;
  const abs = Math.abs(diff);
  const suffix = diff >= 0 ? "ago" : "from now";
  if (abs < 5) return "just now";
  if (abs < 60) return `${Math.round(abs)}s ${suffix}`;
  if (abs < 3600) return `${Math.round(abs / 60)}m ${suffix}`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h ${suffix}`;
  return `${Math.round(abs / 86400)}d ${suffix}`;
}

/** Local date and time with seconds; an em dash when the timestamp is missing. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Compact interval label: `30s`, `5m`, `1h`. */
export function formatInterval(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

/** Time windows offered by range pickers. */
export const TIME_RANGES = [
  { key: "1h", label: "1 hour", hours: 1 },
  { key: "6h", label: "6 hours", hours: 6 },
  { key: "24h", label: "24 hours", hours: 24 },
  { key: "7d", label: "7 days", hours: 24 * 7 },
] as const;
export type TimeRangeKey = (typeof TIME_RANGES)[number]["key"];

/** ISO timestamp for the start of the time window `key` (defaults to 24 hours). */
export function rangeToSince(key: TimeRangeKey, now: Date = new Date()): string {
  const r = TIME_RANGES.find((t) => t.key === key) ?? TIME_RANGES[2];
  return new Date(now.getTime() - r.hours * 3600_000).toISOString();
}

/** Build a query string, dropping empty values. */
export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Assessments describe the selected time window, not only the current moment. */
export const ASSESSMENT_TEXT: Record<string, string> = {
  healthy: "Healthy in this window",
  incomplete: "Incomplete: a region missed most of its checks",
  degraded: "Below 95% success (single region)",
  regional: "Regional: some regions below 95% success",
  global: "Global: every region below 95% success",
  no_data: "No measurements in this window",
};

/** Target as shown in lists: adds the port for TCP and the record type for DNS monitors. */
export function describeTarget(m: { type: string; target: string; settings: Record<string, unknown> }): string {
  if (m.type === "tcp") return `${m.target}:${m.settings.port}`;
  if (m.type === "dns") return `${m.target} (${m.settings.record_type ?? "A"})`;
  return m.target;
}
