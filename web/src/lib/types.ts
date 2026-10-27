// Mirrors the public API schemas (see /openapi.json).

export type MonitorState = "unknown" | "up" | "degraded" | "down" | "paused";
export type CheckType = "http" | "dns" | "tcp" | "icmp";

export type Page<T> = { items: T[]; next_cursor: string | null };

export type Monitor = {
  id: string;
  name: string;
  type: CheckType;
  target: string;
  settings: Record<string, unknown>;
  interval_seconds: number;
  timeout_ms: number;
  latency_threshold_ms: number | null;
  regions: string[];
  paused: boolean;
  state: MonitorState;
  state_changed_at: string | null;
  last_result_at: string | null;
  config_version: number;
  created_at: string;
  updated_at: string;
};

export type Measurement = {
  id: string;
  job_id: string;
  monitor_id: string;
  agent_id: string | null;
  region: string;
  check_type: CheckType;
  target: string;
  measured_at: string;
  ingested_at: string;
  late: boolean;
  success: boolean;
  failure_category: string | null;
  error_message: string | null;
  latency_ms: number | null;
  http_status: number | null;
  dns_ms: number | null;
  connect_ms: number | null;
  tls_ms: number | null;
  ttfb_ms: number | null;
  total_ms: number | null;
  packet_loss_pct: number | null;
  rtt_avg_ms: number | null;
  remote_address: string | null;
  details: Record<string, unknown>;
};

export type LatencyStats = {
  sample_count: number;
  min_ms: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  avg_ms: number | null;
};

export type GroupPerformance = {
  key: string;
  region: string;
  agent_id: string | null;
  agent_name: string | null;
  total: number;
  successes: number;
  success_rate: number | null;
  missing: number;
  latency: LatencyStats;
  packet_loss_avg_pct: number | null;
  last_measured_at: string | null;
  insufficient_data: boolean;
};

export type RegionalPerformance = {
  monitor_id: string;
  since: string;
  until: string;
  overall: GroupPerformance;
  regions: GroupPerformance[];
  agents: GroupPerformance[];
  failure_categories: Record<string, number>;
  assessment: "healthy" | "incomplete" | "degraded" | "regional" | "global" | "no_data";
  slow_regions: string[];
};

export type Timeseries = {
  bucket_seconds: number;
  series: { key: string; points: { t: string; total: number; successes: number; avg_ms: number | null; p95_ms: number | null }[] }[];
};

export type Incident = {
  id: string;
  monitor_id: string;
  rule_id: string;
  status: "open" | "resolved";
  kind: string;
  scope: string;
  affected_regions: string[];
  summary: string;
  opened_at: string;
  resolved_at: string | null;
};

export type IncidentEvent = { id: string; kind: string; message: string; created_at: string };

export type AlertRule = {
  id: string;
  monitor_id: string;
  kind: "unavailable" | "http_status" | "latency" | "packet_loss";
  threshold: number | null;
  consecutive_breaches: number;
  recovery_successes: number;
  min_regions: number;
  enabled: boolean;
  firing_regions: string[];
};

export type Agent = {
  id: string;
  name: string;
  region: string;
  country_code: string | null;
  city: string | null;
  provider: string | null;
  version: string;
  capabilities: string[];
  status: "online" | "offline" | "disabled";
  last_heartbeat_at: string | null;
  registered_at: string;
};

export type Location = {
  region: string;
  country_codes: string[];
  cities: string[];
  agents_total: number;
  agents_online: number;
  capabilities: string[];
};

export type Overview = {
  window_hours: number;
  monitors: Record<string, number>;
  failing_monitors: Monitor[];
  open_incidents: number;
  recent_incidents: Incident[];
  availability_pct: number | null;
  latency: LatencyStats;
  recent_measurements: Measurement[];
  agents: Record<string, number>;
  regions: GroupPerformance[];
};

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  key?: string;
};

export type Channel = { id: string; name: string; url: string; enabled: boolean; created_at: string; signing_secret?: string };
export type Member = { user_id: string; email: string; name: string; role: string; created_at: string };
export type MonitorEvent = { at: string; source: "incident" | "audit"; kind: string; message: string };
