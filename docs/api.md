# API guide

Base URL: `http://localhost:8000` locally. Interactive documentation is served at
`/docs` (Swagger UI) and `/redoc`; the machine-readable spec is at
`/openapi.json` and committed as [openapi.json](openapi.json) (`make openapi`).

## Conventions

* **Auth:** `Authorization: Bearer pak_...` (API key, bound to one workspace).
  Dashboard sessions (`pas_...`) also need `X-Workspace-Id: <uuid>`.
* **Versioning:** all public endpoints are under `/v1`. Additive changes only
  within a version.
* **Pagination:** list endpoints take `limit` and `cursor` and return
  `{"items": [...], "next_cursor": "..." | null}`. Cursors are opaque.
* **Errors:** `{"error": {"code": "...", "message": "...", "details": [...]}}`.
  Codes: `validation_error` (422, `details[].loc` names the field),
  `unauthorized` (401), `forbidden` (403), `not_found` (404, also for other
  tenants' resources), `limit_exceeded` (403), `rate_limited` (429 + `Retry-After`).
* **Units:** milliseconds, percent 0–100, UTC ISO 8601 timestamps; `null` means
  not applicable or not measured.

## Endpoints

| Area | Endpoints |
| --- | --- |
| Auth | `POST /v1/auth/register`, `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/me` |
| Workspaces | `GET/POST /v1/workspaces`, `GET/POST /v1/members`, `DELETE /v1/members/{user_id}` |
| API keys (owner session) | `GET/POST /v1/api-keys`, `DELETE /v1/api-keys/{id}`, `POST /v1/api-keys/{id}/rotate` |
| Monitors | `GET/POST /v1/monitors`, `GET/PATCH/DELETE /v1/monitors/{id}`, `POST /v1/monitors/{id}/pause`, `POST /v1/monitors/{id}/resume` |
| Analytics | `GET /v1/monitors/{id}/regional-performance`, `GET /v1/monitors/{id}/timeseries`, `GET /v1/monitors/{id}/events`, `GET /v1/overview` |
| Measurements | `GET /v1/measurements?monitor_id&region&agent_id&check_type&success&since&until&limit&cursor` |
| Locations | `GET /v1/locations`, `GET /v1/agents` |
| Alerting | `GET/POST /v1/alert-rules`, `GET/PATCH/DELETE /v1/alert-rules/{id}`, `GET /v1/incidents`, `GET /v1/incidents/{id}` |
| Notifications | `GET/POST /v1/notification-channels`, `PATCH/DELETE /v1/notification-channels/{id}`, `POST /v1/notification-channels/{id}/test`, `GET /v1/notification-deliveries` |
| Audit (owner) | `GET /v1/audit-events` |
| Agent protocol | `POST /agent/v1/register`, `/heartbeat`, `/jobs/claim`, `/results` (agent tokens only) |
| Ops | `GET /healthz`, `GET /readyz`, `GET /metrics` |

## Examples

```bash
API=http://localhost:8000
KEY=pak_...   # Settings -> API keys
H="Authorization: Bearer $KEY"

# Available regions
curl -s -H "$H" $API/v1/locations

# HTTP monitor from two regions with a latency threshold
curl -s -H "$H" -H 'Content-Type: application/json' -X POST $API/v1/monitors -d '{
  "name": "Public API",
  "type": "http",
  "target": "https://api.example.com/health",
  "interval_seconds": 60,
  "timeout_ms": 10000,
  "regions": ["eu-central", "us-east"],
  "latency_threshold_ms": 800,
  "settings": {"method": "GET", "expected_status_codes": ["2xx"], "follow_redirects": true}
}'

# Other types
#   dns:  {"type":"dns","target":"example.com","settings":{"record_type":"AAAA","nameserver":"1.1.1.1"}}
#   tcp:  {"type":"tcp","target":"db.example.com","settings":{"port":5432}}
#   icmp: {"type":"icmp","target":"example.com","settings":{"packet_count":5}}

# Alert after 3 consecutive failures in at least 2 regions; recover after 2 successes
curl -s -H "$H" -H 'Content-Type: application/json' -X POST $API/v1/alert-rules -d '{
  "monitor_id": "<id>", "kind": "unavailable",
  "consecutive_breaches": 3, "recovery_successes": 2, "min_regions": 2
}'

# Latency rule
#   {"monitor_id":"<id>","kind":"latency","threshold":800,"consecutive_breaches":5}

# Regional comparison for the last 6 hours
curl -s -H "$H" "$API/v1/monitors/<id>/regional-performance?since=$(date -u -d '-6 hours' +%FT%TZ)"

# Latency time series per agent, 5-minute buckets
curl -s -H "$H" "$API/v1/monitors/<id>/timeseries?group_by=agent&bucket_seconds=300"

# Failed measurements, paginated
curl -s -H "$H" "$API/v1/measurements?monitor_id=<id>&success=false&limit=100"
curl -s -H "$H" "$API/v1/measurements?monitor_id=<id>&success=false&limit=100&cursor=<next_cursor>"

# Open incidents
curl -s -H "$H" "$API/v1/incidents?status=open"

# Webhook (owner session), verify with the returned signing_secret
curl -s -H "Authorization: Bearer pas_..." -H "X-Workspace-Id: <ws>" -H 'Content-Type: application/json' \
  -X POST $API/v1/notification-channels -d '{"name":"on-call","url":"https://hooks.example.com/probeatlas"}'
```

### Regional performance response (abridged)

```json
{
  "assessment": "regional",
  "slow_regions": [],
  "regions": [
    {"region": "eu-central", "total": 360, "successes": 251, "success_rate": 0.6972, "missing": 0,
     "latency": {"sample_count": 251, "min_ms": 18.2, "p50_ms": 24.9, "p95_ms": 61.0, "p99_ms": 140.3, "avg_ms": 29.4},
     "insufficient_data": false},
    {"region": "us-east", "total": 3, "successes": 3, "success_rate": 1.0, "missing": 2,
     "latency": {"sample_count": 3, "min_ms": 88.0, "p50_ms": null, "p95_ms": null, "p99_ms": null, "avg_ms": 90.1},
     "insufficient_data": true}
  ],
  "failure_categories": {"http_status": 102, "timeout": 7}
}
```

### Webhook payload

Headers: `X-ProbeAtlas-Event`, `X-ProbeAtlas-Delivery`, `X-ProbeAtlas-Timestamp`,
`X-ProbeAtlas-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<body>">`.

```json
{
  "event": "incident.opened",
  "incident": {"id": "…", "status": "open", "kind": "unavailable", "scope": "regional",
               "affected_regions": ["eu-central"], "summary": "Public API: checks failing in eu-central",
               "opened_at": "2026-10-27T15:04:30Z", "resolved_at": null},
  "monitor": {"id": "…", "name": "Public API", "type": "http", "target": "https://api.example.com/health"},
  "delivery_id": "…",
  "sent_at": "2026-10-27T15:04:31Z"
}
```

Verification (Python):

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(secret.encode(), f"{ts}.{raw_body}".encode(), hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-ProbeAtlas-Signature"])
```

Reject requests whose timestamp is more than a few minutes old to prevent replay.

## Failure categories

`dns_error`, `dns_nxdomain`, `dns_no_answer`, `timeout`, `connection_refused`,
`connection_reset`, `network_unreachable`, `tls_error`, `tls_certificate`,
`protocol_error`, `http_status`, `redirect_limit`, `packet_loss`,
`blocked_target`, `unsupported`, `internal_error`.
