import type { Metadata } from "next";
import Link from "next/link";
import { LocationsTable, Section, SiteFooter, SiteHeader } from "@/components/Marketing";
import { contactEmail, hasSessionCookie, publicLocations } from "@/lib/server";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "ProbeAtlas: see your infrastructure the way the internet does",
  description:
    "Availability, DNS, TCP, TLS, and HTTP timings of your public endpoints, measured from agents on independent cloud networks. API-first, built for SRE and platform teams.",
};

const QUESTIONS = [
  ["Is it reachable everywhere?", "Availability of each endpoint from every monitoring location, with failures categorized instead of lumped together."],
  ["Where is it slow?", "Latency per region side by side: minimum, P50, P95, and P99, computed only when there are enough samples to mean something."],
  ["Which layer is degrading?", "DNS resolution, TCP connect, TLS handshake, time to first byte, and total time are measured separately on every HTTP check."],
  ["Global, regional, or one network?", "Incidents are classified by where they happen, so one provider's routing problem does not look like an outage."],
  ["What changed over time?", "Raw results are kept with both the agent's measurement time and the server's ingestion time; gaps are shown as missing, never as success."],
];

const CHECKS = [
  ["HTTP and HTTPS", "Status code against your expected codes, DNS, connect, TLS, time to first byte, total time; redirects followed and re-validated."],
  ["DNS", "Resolution time and answers for A, AAAA, CNAME, MX, TXT, and NS records, optionally against a specific nameserver."],
  ["TCP", "Connection time to any public port."],
  ["ICMP", "Round-trip time and packet loss, where the agent's host allows unprivileged ping."],
];

const EXAMPLE = `curl -s https://api.<your-deployment>/v1/monitors \\
  -H "Authorization: Bearer $PROBEATLAS_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"name": "api", "type": "http", "target": "https://api.example.com/health",
       "regions": ["eu-west", "us-east"], "interval_seconds": 60}'

probeatlas monitors list            # the CLI prints JSON too`;

/** Public landing page: what ProbeAtlas answers, the live list of monitoring locations, and how to get access. */
export default async function LandingPage() {
  const [signedIn, locations] = await Promise.all([hasSessionCookie(), publicLocations()]);
  const contact = contactEmail();
  const online = (locations ?? []).filter((l) => l.agents_online > 0);
  const networks = new Set(online.flatMap((l) => l.providers));

  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader signedIn={signedIn} />
      <main className="flex-1">
        <section className="border-b border-slate-200 bg-white">
          <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6 sm:py-20">
            <p className="text-sm font-medium text-sky-700">Internet infrastructure monitoring for engineers</p>
            <h1 className="mt-3 max-w-3xl text-3xl font-semibold tracking-tight text-slate-900 sm:text-5xl">
              See your infrastructure the way the internet does.
            </h1>
            <p className="mt-5 max-w-2xl text-lg text-slate-600">
              ProbeAtlas measures availability and DNS, TCP, TLS, and HTTP timings of your public endpoints from
              monitoring agents on independent cloud networks, and tells you whether a slowdown is global,
              regional, or one network&apos;s problem.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              {contact ? (
                <a
                  href={`mailto:${contact}?subject=${encodeURIComponent("ProbeAtlas access request")}`}
                  className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
                >
                  Request access
                </a>
              ) : null}
              <Link
                href={signedIn ? "/dashboard" : "/login"}
                className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50"
              >
                {signedIn ? "Open dashboard" : "Sign in"}
              </Link>
            </div>
            <p className="mt-4 text-sm text-slate-500">
              Sign-up is by invitation while we work with our first design partners.
            </p>
            {online.length > 0 && (
              <p className="mt-6 text-sm text-slate-700">
                Measuring right now from <strong>{online.length}</strong> {online.length === 1 ? "location" : "locations"}
                {networks.size > 0 && (
                  <>
                    {" "}on <strong>{networks.size}</strong> {networks.size === 1 ? "network" : "networks"}
                  </>
                )}
                . <a href="#locations" className="text-sky-700 hover:underline">See where</a>
              </p>
            )}
          </div>
        </section>

        <Section title="Five questions, answered with real measurements">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {QUESTIONS.map(([q, a]) => (
              <div key={q} className="rounded-lg border border-slate-200 bg-white p-4">
                <h3 className="font-medium text-slate-900">{q}</h3>
                <p className="mt-1 text-sm text-slate-600">{a}</p>
              </div>
            ))}
          </div>
        </Section>

        <Section
          id="locations"
          title="Where we measure from"
          lead="This list comes live from the platform: the agents it runs right now, with the location their operator configured. Coverage grows as locations are added; we never infer locations from IP addresses."
        >
          <LocationsTable locations={locations} />
        </Section>

        <Section title="Checks">
          <div className="grid gap-4 sm:grid-cols-2">
            {CHECKS.map(([name, detail]) => (
              <div key={name} className="rounded-lg border border-slate-200 bg-white p-4">
                <h3 className="font-medium text-slate-900">{name}</h3>
                <p className="mt-1 text-sm text-slate-600">{detail}</p>
              </div>
            ))}
          </div>
        </Section>

        <Section
          title="API-first, from day one"
          lead="Everything the dashboard does goes through a versioned REST API with an OpenAPI spec, API keys you can rotate, and a CLI with JSON output, so monitors live next to the rest of your configuration."
        >
          <pre className="overflow-x-auto rounded-lg bg-slate-900 p-4 text-xs leading-relaxed text-slate-100">
            <code>{EXAMPLE}</code>
          </pre>
          <ul className="mt-6 grid gap-3 text-sm text-slate-700 sm:grid-cols-2">
            <li>Fields that were not measured are <code className="rounded bg-slate-100 px-1">null</code>, never zero.</li>
            <li>Percentiles need enough samples (P95 at least 20, P99 at least 100); otherwise they are left out.</li>
            <li>Every workspace is isolated; API keys are stored hashed and shown once.</li>
            <li>Agents only contact public addresses and re-check every redirect.</li>
          </ul>
        </Section>
      </main>
      <SiteFooter contact={contact} />
    </div>
  );
}
