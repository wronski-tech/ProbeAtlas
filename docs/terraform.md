# Terraform integration (design, not yet implemented)

The public API was shaped so a Terraform provider can be a thin client:

* Every resource has a stable server-generated `id` and is addressable at
  `/v1/<collection>/{id}` with `GET`, `PATCH`, and `DELETE`.
* Create returns the full object; reads are side-effect free.
* `PATCH` is a partial update; unknown fields are rejected (drift is visible).
* Immutable fields are explicit (`type` on monitors, `monitor_id` and `kind` on
  alert rules), so the provider can mark them `ForceNew`.
* Server-normalized values (lowercased hostnames, deduplicated regions,
  defaulted settings) are returned on create, so the provider can store them and
  suppress diffs.
* Authentication is a workspace-scoped API key, which matches a provider block.
* No hidden side effects: creating a monitor does not create alert rules (the
  dashboard does that with a second call).

## Proposed resources

```hcl
provider "probeatlas" {
  api_url = "https://api.probeatlas.example"
  api_key = var.probeatlas_api_key           # or PROBEATLAS_API_KEY
}

data "probeatlas_locations" "all" {}         # GET /v1/locations

resource "probeatlas_monitor" "api" {
  name                 = "Public API"
  type                 = "http"              # ForceNew
  target               = "https://api.example.com/health"
  interval_seconds     = 60
  timeout_ms           = 10000
  regions              = ["eu-central", "us-east"]
  latency_threshold_ms = 800
  paused               = false              # maps to /pause and /resume

  http {
    method                = "GET"
    expected_status_codes = ["2xx"]
    follow_redirects      = true
    max_redirects         = 5
  }
  # dns  { record_type = "A"  nameserver = "1.1.1.1" }
  # tcp  { port = 5432 }
  # icmp { packet_count = 3 }
}

resource "probeatlas_alert_rule" "api_down" {
  monitor_id           = probeatlas_monitor.api.id   # ForceNew
  kind                 = "unavailable"                # ForceNew
  consecutive_breaches = 3
  recovery_successes   = 2
  min_regions          = 2
}

resource "probeatlas_notification_channel" "oncall" {
  name = "on-call"
  url  = "https://hooks.example.com/probeatlas"
  # signing_secret: computed, sensitive, only known after create
}
```

Mapping:

| Resource | Create | Read | Update | Delete |
| --- | --- | --- | --- | --- |
| `probeatlas_monitor` | `POST /v1/monitors` | `GET /v1/monitors/{id}` | `PATCH` (+ `/pause` or `/resume` when `paused` changes) | `DELETE` |
| `probeatlas_alert_rule` | `POST /v1/alert-rules` | `GET /v1/alert-rules/{id}` | `PATCH` | `DELETE` |
| `probeatlas_notification_channel` | `POST /v1/notification-channels` | `GET` list + filter | `PATCH` | `DELETE` |
| `data.probeatlas_locations` | — | `GET /v1/locations` | — | — |

## API gaps to close before writing the provider

1. `GET /v1/notification-channels/{id}` for single-channel reads.
2. Optional `Idempotency-Key` header on `POST` so retried applies cannot create
   duplicates.
3. `PUT` or `If-Match` with `config_version` for optimistic concurrency.
4. Channel selection per alert rule (today rules notify every enabled channel).
5. An import-friendly `GET /v1/monitors?name=` filter.
