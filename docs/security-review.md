# Threat model and security review

This page has three parts:

1. The threat model.
2. The results of the internal review from 2026-10-27, with the status of each finding.
3. A scope and briefing package for an external penetration test.

The controls themselves are described in [security.md](security.md).

> **Status:** the internal review was done by the engineering team, using code reading, local exploit
> attempts, and regression tests. **No external review or penetration test has taken place yet.** Treat
> this as the input to that review, not a substitute for it.

## 1. Threat model

### Assets

| Asset | Where | Why it matters |
| --- | --- | --- |
| Tenant configuration (targets, settings, alert rules) | `monitors`, `alert_rules` | Reveals customer infrastructure |
| Measurement history and incidents | `results`, `incidents` | Customer data; integrity drives alerting |
| Credentials | `user_sessions`, `api_keys`, `agents`, `enrollment_tokens` (SHA-256 hashes); `users.password_hash` (scrypt) | Account and agent takeover |
| Webhook signing secrets and URLs | `notification_channels` (plaintext) | Forged notifications; URLs often embed tokens |
| Agent network position | every agent host | SSRF into the agent's network and cloud metadata; abuse as a traffic source |
| Availability of scheduling and alerting | worker, agents | A stalled worker silently stops monitoring for **every** tenant |

### Actors

* **Anonymous internet user.** Can reach the web, API, and agent protocol endpoints. Self-registration is
  on by default.
* **Tenant user or API key.** An authenticated member of one workspace. It is the most capable untrusted
  actor: it controls targets, webhook URLs, and alert rules, and can run any public host it chooses as a
  check target.
* **Malicious target or webhook receiver.** Controls the responses that agents and the worker read.
* **Compromised agent.** Holds a valid agent token for one region.
* **Operator.** Has database access, the admin CLI, and enrollment tokens. Trusted.

### Trust boundaries

```
 browser ─(cookie, CSRF header)─▶ web proxy ─(Bearer pas_, X-Workspace-Id)─▶ API ─▶ PostgreSQL
 CLI / API client ─(Bearer pak_)───────────────────────────────────────────▶ API
 agent ─(Bearer paa_ / pae_ for enrollment)────────────────────────────────▶ API (/agent/v1)
 agent ─(customer-chosen destination, policy-checked)──▶ internet
 worker ─(webhook URL, policy-checked, signed)─────────▶ internet
```

### Main threats and controls

| Threat | Control | Tests |
| --- | --- | --- |
| SSRF into private networks or metadata via targets, redirects, DNS rebinding, IPv6 transition forms | `netpolicy` (syntax + every resolved IP, connect to the validated IP), redirect revalidation, port policy | `test_netpolicy.py`, `test_checks.py` |
| Slow or never-ending responses tying up agents and the worker | hard per-request deadline (watchdog) in `safehttp`; webhooks run in their own worker thread | `test_checks.py::test_http_timeout_is_a_hard_deadline_even_when_the_server_drips_bytes` |
| Cross-tenant read or write | `Principal` per request, `tenant_get`, every query filters on workspace, agents never send tenant ids | `test_tenancy.py`, `test_ingestion.py` |
| Credential theft from the database or logs | hashed tokens, scrypt passwords, log redaction (message and fields), `hide_parameters` | `test_auth.py` |
| CSRF and login CSRF in the dashboard | httpOnly SameSite=Lax cookie; every mutating web route requires `x-probeatlas-csrf: 1` | `web/src/lib/proxy.test.ts` (path rules); manual |
| Result forgery or replay | results accepted only for jobs leased to that agent; unique `job_id`; timestamp skew check | `test_ingestion.py` |
| Abuse of agents as a scanner or flooder | fixed check types, no custom payloads, port policy, per-workspace quotas and minimum interval | `test_monitors.py` |

### Accepted risks (by design, for now)

* **A compromised agent is trusted for its region.** It can see every tenant's targets in that region
  and report false results for them. Mitigations: region-bound single-use enrollment tokens,
  `probeatlas-admin disable-agent`, audit events. Planned: per-agent token rotation, and cross-checking
  results across agents.
* **Enrollment tokens without a region** let an agent create a new region label. The bootstrap token
  (`region=None`, 1000 uses) is for local development only and must be unset in production.
* **Outbound abuse.** One email can create up to 10 workspaces × 25 monitors × 10 regions at 30 s
  intervals, against any public host and port. Sign-up controls and per-destination volume limits are
  on the production checklist.

## 2. Internal review (2026-10-27)

Scope: SSRF guard, tenant isolation, agent protocol, web proxy, and secrets in logs, reviewed at commit
`9c60975`; the fixes ship in the change that adds this document. Severity reflects impact for a multi-tenant SaaS with
self-registration enabled.

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| C1 | Critical | `safehttp` set socket timeouts per phase, but those apply to each `recv()`. A server that sent one byte at a time held a check or webhook delivery indefinitely; reproduced at 10 s for a 1 s timeout. One tenant could stall the worker (scheduling and alerting for everyone), the agents in a region, and the API thread pool through `/test`. | **Fixed.** A watchdog shuts the socket down at the deadline. Regression test added. |
| H1 | High | Webhook delivery ran inside the scheduling tick (a batch of 50 × 10 s), the 60 s delivery lease was shorter than a batch (duplicate sends with several workers), and channels per workspace were unlimited. | **Fixed.** Delivery runs in its own worker thread, the lease covers the batch, and there are at most 10 channels per workspace (with a test). |
| H2 | High | `getaddrinfo` runs in a shared 8-thread pool; abandoned lookups keep their thread for several seconds, so one tenant could starve other tenants' DNS resolution. | **Mitigated.** The pool is now 64 threads. **Open:** resolve A/AAAA with dnspython and a hard lifetime. |
| M1 | Medium | The API sees the web container's IP for every dashboard login, so 20 failed logins per minute from anyone lock the dashboard for everyone. Audit IPs for dashboard actions are also the web server's. | **Open.** The web tier should forward the client IP, and the API should trust `X-Forwarded-For` only from the web tier (`FORWARDED_ALLOW_IPS`). |
| M2 | Medium | The proxy's traversal check missed encoded slashes (`/api/proxy/v1/..%2Fmetrics` reached `/metrics`). `/metrics` is also served on the public API port. | **Fixed:** the proxy now allows only plain URL-safe `/v1` segments (with tests). **Open:** move API metrics to an internal port; until then, block `/metrics` at the load balancer. |
| L1 | Low | Login CSRF: `POST /api/session` did not require the CSRF header, so a cross-site `text/plain` form could sign a victim into an attacker's account. | **Fixed.** Sign-in, registration, and sign-out require `x-probeatlas-csrf: 1`. |
| L2 | Low | Owners can add any registered user to their workspace without consent, and the 404/201 difference enumerates which emails have accounts. | **Open.** Replace with an invitation and acceptance flow that responds the same for every email. |
| L3 | Low | IPv4-compatible (`::7f00:1`), SIIT (`::ffff:0:a.b.c.d`), local NAT64 (`64:ff9b:1::/48`), and site-local (`fec0::/10`) addresses were treated as public. | **Fixed**, with tests. |
| L4 | Low | DNS checks return CNAME/MX/NS/TXT answers unfiltered, and `getaddrinfo` applies resolver search domains, which can confirm internal names (for example `redis.default` inside Kubernetes). | **Open.** Resolve absolute names (trailing dot), use one generic "blocked" message, and run agents outside private clusters. |
| L5 | Low | A heartbeat racing `disable-agent` could re-enable the agent. | **Fixed.** The heartbeat update skips disabled agents. |
| L6 | Low | Structured log fields were not redacted, and SQL errors could log bound parameters such as password hashes. | **Fixed:** fields are redacted and the engine uses `hide_parameters=True`. Test added. |
| L7 | Low | Members and API keys can read full webhook URLs, which often embed tokens. | **Open.** Mask URLs for anyone who is not an owner. |

The review found these areas sound: literal, encoded, and IPv4-mapped IP handling; resolution-time
checks and connecting to the validated IP; redirect revalidation; TLS hostname verification; tenant
filters in every `/v1` router; ingestion ownership and idempotency checks; owner-only gating of keys,
members, channels, and the audit log; escaped rendering in the dashboard; and error responses that do
not echo input.

## 3. External penetration test package

**Goal:** find vulnerabilities that would allow cross-tenant access, SSRF from agents or the worker,
credential compromise, or denial of monitoring, before opening sign-ups to design partners.

**In scope**

* Web dashboard (`:3000`), including `/api/session`, `/api/session/workspace`, and `/api/proxy/*`.
* Public API `/v1/*` (OpenAPI: `docs/openapi.json`).
* Agent protocol `/agent/v1/*`, with a test enrollment token.
* The agent's outbound behavior: targets, redirects, DNS, ICMP, TCP.
* Webhook delivery from the worker.
* Container hardening of the published images.

**Out of scope:** denial of service by raw volume, the hosting provider's infrastructure, social
engineering.

**Environment:** a dedicated staging deployment built from `main`, with `PROBEATLAS_ENV=production`,
private targets disabled, and two agents in different regions. Do not test against the local Compose
demo, which deliberately enables `PROBEATLAS_ALLOW_PRIVATE_TARGETS`.

**Provided to testers**

* Two workspaces with an owner and a member each, and an API key per workspace.
* A region-bound enrollment token for one extra agent, plus permission to run that agent.
* An attacker-controlled HTTP server (or permission to use their own).
* This page, [security.md](security.md), [architecture.md](architecture.md), and read access to the
  repository.

**Priority test cases**

1. SSRF bypasses: DNS rebinding, IPv6 and transition forms, redirect chains, unusual URL parsing,
   nameserver settings, and ICMP/TCP targets.
2. Cross-tenant access through every `/v1` resource, cursors, and query filters, with both session and
   API key authentication.
3. The agent protocol: claiming or submitting other agents' jobs, replay, oversized or malformed
   payloads, and abuse of the enrollment token.
4. Session handling: CSRF, cookie flags, workspace switching, logout, and API key rotation and
   revocation.
5. Resource exhaustion that a single tenant can cause: slow responses, slow DNS, many channels or
   monitors.
6. Information leaks in errors, logs (with operator cooperation), and the dashboard.

**Deliverable:** findings with severity, reproduction steps, and suggested fixes. Each fixed finding
gets a regression test and an entry in the table above.
