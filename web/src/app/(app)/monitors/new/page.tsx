"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import type { Monitor } from "@/lib/types";
import { Card, PageHeader } from "@/components/ui";
import { MonitorForm } from "@/components/MonitorForm";

export default function NewMonitorPage() {
  const router = useRouter();
  const [defaultRule, setDefaultRule] = useState(true);

  return (
    <>
      <PageHeader title="New monitor" subtitle="Checks start running within a few seconds of creation." />
      <Card>
        <MonitorForm
          submitLabel="Create monitor"
          extra={
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input type="checkbox" checked={defaultRule} onChange={(e) => setDefaultRule(e.target.checked)} />
              Alert when checks fail 3 times in a row in any region (recover after 2 successes)
            </label>
          }
          onSubmit={async (v) => {
            const m = await api<Monitor>("/v1/monitors", { method: "POST", body: v });
            if (defaultRule) {
              await api("/v1/alert-rules", { method: "POST", body: { monitor_id: m.id, kind: "unavailable", consecutive_breaches: 3, recovery_successes: 2 } });
            }
            router.push(`/monitors/${m.id}`);
          }}
        />
      </Card>
    </>
  );
}
