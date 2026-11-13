import Link from "next/link";
import type { ReactNode } from "react";
import type { Location } from "@/lib/types";

const REPO = "https://github.com/wronski-tech/ProbeAtlas";

/** Top bar of the public pages: product name, a link to the probes page, and the sign-in or dashboard button. */
export function SiteHeader({ signedIn }: { signedIn: boolean }) {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
        <Link href="/" className="text-base font-semibold tracking-tight text-slate-900">
          ProbeAtlas
        </Link>
        <nav aria-label="Site" className="flex items-center gap-4 text-sm">
          <Link href="/probes" className="text-slate-600 hover:text-slate-900">
            About our probes
          </Link>
          <a href={`${REPO}/blob/main/docs/api.md`} className="hidden text-slate-600 hover:text-slate-900 sm:inline">
            API docs
          </a>
          <Link
            href={signedIn ? "/dashboard" : "/login"}
            className="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white hover:bg-slate-700"
          >
            {signedIn ? "Open dashboard" : "Sign in"}
          </Link>
        </nav>
      </div>
    </header>
  );
}

/** Footer of the public pages with the probes page, docs, source, and the contact address when configured. */
export function SiteFooter({ contact }: { contact: string | null }) {
  return (
    <footer className="border-t border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-6 text-sm text-slate-500 sm:px-6">
        <span>ProbeAtlas: internet infrastructure monitoring from operator-placed agents.</span>
        <nav aria-label="Footer" className="flex flex-wrap gap-4">
          <Link href="/probes" className="hover:text-slate-900">Probes and abuse reports</Link>
          <a href={`${REPO}/blob/main/docs/api.md`} className="hover:text-slate-900">API reference</a>
          <a href={REPO} className="hover:text-slate-900">Source</a>
          {contact && <a href={`mailto:${contact}`} className="hover:text-slate-900">{contact}</a>}
        </nav>
      </div>
    </footer>
  );
}

/** A titled page section with an optional lead paragraph. */
export function Section({ id, title, lead, children }: { id?: string; title: string; lead?: ReactNode; children: ReactNode }) {
  return (
    <section id={id} className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
      <h2 className="text-xl font-semibold tracking-tight text-slate-900">{title}</h2>
      {lead && <p className="mt-2 max-w-3xl text-slate-600">{lead}</p>}
      <div className="mt-6">{children}</div>
    </section>
  );
}

/** Join the non-empty labels of a location ("Sydney, AU"), or say that none were configured. */
function place(loc: Location): string {
  return [loc.cities.join(" / "), loc.country_codes.join(" / ")].filter(Boolean).join(", ") || "location not set";
}

/**
 * The deployment's real monitoring locations, straight from the API. Says so plainly when there are none or
 * the data is unavailable, instead of showing placeholder coverage.
 */
export function LocationsTable({ locations }: { locations: Location[] | null }) {
  if (locations === null) {
    return <p className="text-sm text-slate-600">Location data is unavailable right now. Please try again shortly.</p>;
  }
  if (locations.length === 0) {
    return <p className="text-sm text-slate-600">No monitoring agents are enrolled yet.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="px-4 py-2 font-medium">Region</th>
            <th scope="col" className="px-4 py-2 font-medium">Location</th>
            <th scope="col" className="px-4 py-2 font-medium">Network</th>
            <th scope="col" className="px-4 py-2 font-medium">Agents online</th>
            <th scope="col" className="px-4 py-2 font-medium">Checks</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {locations.map((loc) => (
            <tr key={loc.region}>
              <td className="px-4 py-2 font-mono text-xs text-slate-900">{loc.region}</td>
              <td className="px-4 py-2 text-slate-700">{place(loc)}</td>
              <td className="px-4 py-2 text-slate-700">{loc.providers.join(", ") || "—"}</td>
              <td className="px-4 py-2 tabular text-slate-700">
                <span className={loc.agents_online > 0 ? "text-emerald-700" : "text-slate-500"}>
                  {loc.agents_online} of {loc.agents_total}
                </span>
              </td>
              <td className="px-4 py-2 text-slate-700">{loc.capabilities.join(", ").toUpperCase() || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
