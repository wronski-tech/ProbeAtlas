import type { Metadata } from "next";
import { LocationsTable, Section, SiteFooter, SiteHeader } from "@/components/Marketing";
import { contactEmail, hasSessionCookie, publicLocations } from "@/lib/server";

// The contact address is read at request time from PROBEATLAS_CONTACT_EMAIL.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "About ProbeAtlas probes",
  description: "What ProbeAtlas monitoring agents send, how often, from where, and how to reach their operator.",
};

const SENDS = [
  ["HTTP and HTTPS", "One request with the method the customer chose (GET or HEAD), no body, no cookies, no custom headers, and this User-Agent. At most 64 KiB of the response is read, and it is never stored."],
  ["DNS", "Ordinary queries for one record type, through the agent's configured resolvers or a nameserver the customer named."],
  ["TCP", "A connection is opened and closed again. No data is sent."],
  ["ICMP", "Up to 10 echo requests (ping), 3 by default."],
];

/**
 * Public page linked from the agents' User-Agent: what the probes do, the limits they follow, where they run,
 * and how owners of monitored servers can reach the operator.
 */
export default async function ProbesPage() {
  const [signedIn, locations] = await Promise.all([hasSessionCookie(), publicLocations()]);
  const contact = contactEmail();

  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader signedIn={signedIn} />
      <main className="flex-1">
        <section className="border-b border-slate-200 bg-white">
          <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
            <h1 className="text-3xl font-semibold tracking-tight text-slate-900">About ProbeAtlas probes</h1>
            <p className="mt-4 max-w-3xl text-slate-600">
              If your server receives requests with a User-Agent like{" "}
              <code className="rounded bg-slate-100 px-1 text-sm">ProbeAtlas-Agent/&lt;version&gt; (+&lt;link to this page&gt;)</code>,
              they come from monitoring agents run by the operator of this site. A customer configured your
              hostname or URL as a monitoring target, usually because it is their own service or one they depend on.
            </p>
          </div>
        </section>

        <Section title="What the probes send">
          <div className="grid gap-4 sm:grid-cols-2">
            {SENDS.map(([name, detail]) => (
              <div key={name} className="rounded-lg border border-slate-200 bg-white p-4">
                <h3 className="font-medium text-slate-900">{name}</h3>
                <p className="mt-1 text-sm text-slate-600">{detail}</p>
              </div>
            ))}
          </div>
        </Section>

        <Section title="Limits they follow" lead="Defaults of the platform; an operator can make them stricter.">
          <ul className="list-disc space-y-2 pl-5 text-sm text-slate-700">
            <li>Each monitor runs at most once every 30 seconds per location.</li>
            <li>One customer workspace may send at most 120 checks per minute to a single host, across all of its monitors.</li>
            <li>Every check gives up after at most 30 seconds.</li>
            <li>HTTP checks only use ports 80, 443, and 1024–65535, and follow at most 10 redirects, each one re-checked.</li>
            <li>Probes never contact private, loopback, link-local, or cloud metadata addresses.</li>
            <li>Customers cannot send their own payloads, headers, scripts, or commands.</li>
          </ul>
        </Section>

        <Section
          title="Where the probes run"
          lead="The agents of this deployment right now, with the location and network their operator configured."
        >
          <LocationsTable locations={locations} />
        </Section>

        <Section title="Contact and abuse reports">
          {contact ? (
            <p className="max-w-3xl text-slate-700">
              To ask about or object to checks against your systems, email{" "}
              <a href={`mailto:${contact}?subject=${encodeURIComponent("ProbeAtlas probe report")}`} className="font-medium text-sky-700 hover:underline">
                {contact}
              </a>{" "}
              with the affected hostname or IP address, a few timestamps (with time zone), and the User-Agent or
              source addresses you saw. We will investigate and stop checks that are unwanted.
            </p>
          ) : (
            <p className="max-w-3xl text-slate-700">
              The operator of this deployment has not published a contact address yet.
            </p>
          )}
        </Section>
      </main>
      <SiteFooter contact={contact} />
    </div>
  );
}
