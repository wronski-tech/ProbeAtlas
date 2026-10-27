"use client";

import { useApi } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Agent, Location } from "@/lib/types";
import { Badge, Card, Empty, ErrorBanner, PageHeader, Table, Td } from "@/components/ui";

export default function AgentsPage() {
  const { data: agents, error } = useApi<Agent[]>("/v1/agents", 10_000);
  const { data: locations } = useApi<Location[]>("/v1/locations", 10_000);

  return (
    <>
      <PageHeader
        title="Agents & locations"
        subtitle="Location metadata is assigned by the operator who runs each agent. It is not inferred from IP addresses."
      />
      <ErrorBanner error={error} />
      <div className="space-y-5">
        <Card title="Regions">
          <Table head={["Region", "Location", "Agents online", "Capabilities"]} empty={locations?.length === 0 && <Empty>No regions yet.</Empty>}>
            {locations?.map((l) => (
              <tr key={l.region}>
                <Td className="font-medium">{l.region}</Td>
                <Td>{[l.cities.join(", "), l.country_codes.join(", ")].filter(Boolean).join(" · ") || <span className="text-slate-400">not configured</span>}</Td>
                <Td className="tabular">{l.agents_online} / {l.agents_total}</Td>
                <Td className="text-xs">{l.capabilities.join(", ") || "—"}</Td>
              </tr>
            ))}
          </Table>
        </Card>
        <Card title="Agents">
          <Table head={["Name", "Status", "Region", "Country / city", "Provider", "Version", "Capabilities", "Last heartbeat", "Enrolled"]} empty={agents?.length === 0 && <Empty>No agents enrolled.</Empty>}>
            {agents?.map((a) => (
              <tr key={a.id}>
                <Td className="font-medium" title={a.id}>{a.name}</Td>
                <Td><Badge value={a.status} /></Td>
                <Td>{a.region}</Td>
                <Td>{[a.country_code, a.city].filter(Boolean).join(" / ") || "—"}</Td>
                <Td>{a.provider ?? "—"}</Td>
                <Td className="font-mono text-xs">{a.version}</Td>
                <Td className="text-xs">{a.capabilities.join(", ")}</Td>
                <Td title={formatDateTime(a.last_heartbeat_at)}>{relativeTime(a.last_heartbeat_at)}</Td>
                <Td className="text-xs text-slate-500">{formatDateTime(a.registered_at)}</Td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>
    </>
  );
}
