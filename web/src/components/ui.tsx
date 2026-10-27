"use client";

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { TIME_RANGES, type TimeRangeKey } from "@/lib/format";
import type { ApiError } from "@/lib/api";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("min-w-0 rounded-lg border border-slate-200 bg-white", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const STATE_STYLES: Record<string, string> = {
  up: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  online: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  resolved: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  healthy: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  ok: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  degraded: "bg-amber-50 text-amber-800 ring-amber-600/20",
  regional: "bg-amber-50 text-amber-800 ring-amber-600/20",
  incomplete: "bg-amber-50 text-amber-800 ring-amber-600/20",
  down: "bg-red-50 text-red-700 ring-red-600/20",
  open: "bg-red-50 text-red-700 ring-red-600/20",
  failed: "bg-red-50 text-red-700 ring-red-600/20",
  global: "bg-red-50 text-red-700 ring-red-600/20",
  offline: "bg-slate-100 text-slate-600 ring-slate-500/20",
  unknown: "bg-slate-100 text-slate-600 ring-slate-500/20",
  no_data: "bg-slate-100 text-slate-600 ring-slate-500/20",
  paused: "bg-sky-50 text-sky-700 ring-sky-600/20",
};

export function Badge({ value, label }: { value: string; label?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset", STATE_STYLES[value] ?? STATE_STYLES.unknown)}>
      <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current" />
      {label ?? value.replace("_", " ")}
    </span>
  );
}

export function ResultBadge({ success, category }: { success: boolean; category?: string | null }) {
  return success ? <Badge value="ok" label="success" /> : <Badge value="failed" label={category?.replaceAll("_", " ") ?? "failed"} />;
}

export function Button({ variant = "primary", className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost" }) {
  const styles = {
    primary: "bg-slate-900 text-white hover:bg-slate-700 disabled:bg-slate-400",
    secondary: "border border-slate-300 bg-white text-slate-800 hover:bg-slate-50 disabled:text-slate-400",
    danger: "border border-red-200 bg-white text-red-700 hover:bg-red-50",
    ghost: "text-slate-600 hover:bg-slate-100",
  }[variant];
  return <button className={cx("inline-flex items-center justify-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600", styles, className)} {...props} />;
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

const inputCls = "block rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-xs focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500";

/** Full width unless the caller sets an explicit width class. */
const width = (className?: string) => (/(^|\s)w-/.test(className ?? "") ? "" : "w-full");

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(inputCls, width(props.className), props.className)} />;
}

export function Select({ children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...props} className={cx(inputCls, width(props.className), "pr-8", props.className)}>
      {children}
    </select>
  );
}

export function ErrorBanner({ error }: { error?: ApiError | Error | string | null }) {
  if (!error) return null;
  const msg = typeof error === "string" ? error : "describe" in error ? (error as ApiError).describe() : error.message;
  return (
    <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "good" | "bad" | "warn" }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={cx("mt-1 text-2xl font-semibold tabular-nums", tone === "good" && "text-emerald-700", tone === "bad" && "text-red-700", tone === "warn" && "text-amber-700", !tone && "text-slate-900")}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function RangePicker({ value, onChange }: { value: TimeRangeKey; onChange: (v: TimeRangeKey) => void }) {
  return (
    <div role="group" aria-label="Time range" className="inline-flex rounded-md border border-slate-300 bg-white p-0.5">
      {TIME_RANGES.map((r) => (
        <button
          key={r.key}
          type="button"
          aria-pressed={value === r.key}
          onClick={() => onChange(r.key)}
          className={cx("rounded px-2.5 py-1 text-xs font-medium", value === r.key ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100")}
        >
          {r.key}
        </button>
      ))}
    </div>
  );
}

export function Table({ head, children, empty }: { head: ReactNode[]; children: ReactNode; empty?: ReactNode }) {
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="min-w-full text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            {head.map((h, i) => (
              <th key={i} scope="col" className="whitespace-nowrap px-3 py-2 font-medium first:pl-4 last:pr-4">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
      {empty}
    </div>
  );
}

export function Td({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <td title={title} className={cx("whitespace-nowrap px-3 py-2 text-slate-700 first:pl-4 last:pr-4", className)}>
      {children}
    </td>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <div className="mt-1 text-sm text-slate-500">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function SecretOnce({ label, secret, onDone }: { label: string; secret: string; onDone: () => void }) {
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
      <p className="font-medium text-amber-900">{label}</p>
      <p className="mt-1 text-amber-800">Copy it now. It will not be shown again.</p>
      <code className="mt-2 block break-all rounded bg-white px-2 py-1.5 font-mono text-xs text-slate-900 ring-1 ring-amber-200">{secret}</code>
      <div className="mt-2 flex gap-2">
        <Button variant="secondary" onClick={() => navigator.clipboard?.writeText(secret)}>
          Copy
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}
