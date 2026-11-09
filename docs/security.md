# Security model

## Outbound requests (SSRF and abuse)

Agents and webhook delivery contact customer-supplied destinations. The policy
lives in `src/probeatlas/common/netpolicy.py` and `safehttp.py` and is applied
in two layers:

1. **Configuration time (control plane).** URLs must be `http`/`https` without
   embedded credentials. Hostnames are normalized (IDNA, lowercase) and
   validated. `localhost`, single-label names, `.local`, `.internal`,
   `.localhost`, cloud metadata names, and IP literals in blocked ranges are
   rejected. HTTP ports are limited to 80, 443, and 1024–65535, which blocks
   using HTTP checks against SMTP, SSH, and similar services.
2. **Connection time (agent, authoritative).** The agent resolves the name,
   validates **every** returned address, and connects to the validated address
   itself, so a DNS answer cannot change between check and use (DNS rebinding).
   If any address is blocked, the destination is rejected. TLS still verifies the
   original hostname. Every redirect hop is validated the same way; at most 10
   redirects, and only to `http`/`https`.

Blocked: loopback, RFC 1918 private, link-local, CGNAT (100.64/10), multicast,
unspecified, reserved, and IPv6 equivalents including IPv4-mapped, IPv4-compatible,
SIIT, 6to4, Teredo, NAT64 (well-known and local-use) forms, and site-local `fec0::/10`. Cloud metadata addresses (169.254.169.254, 169.254.170.2,
100.100.100.200, fd00:ec2::254, …) are blocked **even if** an operator
allowlists their range.

Name resolution uses dnspython, not `getaddrinfo`. Each lookup runs in the
calling thread under the check's own deadline, so a tenant pointing targets at
nameservers that never answer cannot starve other tenants' lookups. Names are
always queried as absolute (no resolver search domains), and `localhost` names
map to loopback without DNS (RFC 6761). Agents should run with
`PROBEATLAS_AGENT_NAMESERVERS` set to public recursive resolvers, so the hosting
network's private or split-horizon zones are never consulted. The control plane
has the matching `PROBEATLAS_WEBHOOK_NAMESERVERS`.

DNS checks do not connect to the target. A/AAAA answers that point at blocked
ranges, and CNAME/MX/NS answers that name local or internal hosts, are reported
as `blocked_target` without revealing them, so DNS checks cannot map internal
networks or zones. Custom nameservers must pass the same policy. TXT answers are
returned as-is; with public resolvers they only contain public data.

**Private monitoring is not supported in production.** Two explicit development
switches exist:

* `PROBEATLAS_ALLOW_PRIVATE_TARGETS=true` (control plane) relaxes the hostname
  syntax checks so names like `demo-target` are accepted. IP literals in private
  ranges stay rejected.
* `PROBEATLAS_AGENT_ALLOWED_PRIVATE_CIDRS` (agent) allowlists specific private
  ranges at connection time. The compose file allows only the isolated demo
  network `10.203.10.0/24`; the database and API remain unreachable.

Every HTTP request (checks and webhooks) has a hard overall deadline: a watchdog
closes the connection when the timeout expires, so a server that answers one byte
at a time cannot hold an agent or worker thread past the check timeout.

Other limits: fixed check types with validated settings (no commands, scripts,
custom headers, or request bodies); timeouts capped at 30 s; response bodies
read at most 64 KiB and never stored; minimum interval per workspace (default
30 s); monitor quota per workspace (default 25); at most 10 regions per monitor;
at most 120 checks per minute from one workspace to any one destination host
(`PROBEATLAS_MAX_CHECKS_PER_MINUTE_PER_DESTINATION`, counted as regions × 60 /
interval over active monitors, all check types and ports). The budget is per
workspace so that no tenant can use up another's budget or learn what others
monitor.

## Authentication

* Passwords: scrypt (N=2^14, r=8, p=1) with a per-user salt; login compares
  against a dummy hash for unknown emails to avoid revealing which accounts exist.
* Sessions (`pas_…`), API keys (`pak_<id>_<secret>`), agent tokens (`paa_…`),
  enrollment tokens (`pae_…`) are 256-bit random values. Only SHA-256 digests are
  stored. Secrets are shown once at creation.
* Sign-up is invite-only by default (`PROBEATLAS_REGISTRATION=invite`): an account
  can only be created with a pending invitation for that email, either from a
  workspace owner or from the operator (`probeatlas-admin create-workspace`).
  The account joins the inviting workspace and gets no workspace of its own.
* Workspace membership requires consent: owners create an invitation (a `pai_…`
  token valid for 7 days, shown once) and the invitee joins by accepting it with
  the signed-in account whose email it was sent to. Invitation responses are the
  same whether or not an account exists, so they cannot be used to enumerate
  users, and a user's own first workspace stays their default.
* Webhook URLs, which often embed receiver tokens, are shown in full only to
  owners; members and API keys see scheme and host.
* API keys are bound to one workspace, can expire, and can be revoked or rotated
  (rotation revokes the old key immediately). Only workspace owners using a
  session can create, rotate, or revoke keys, so a leaked key cannot mint more keys.
* The dashboard keeps the session token in an httpOnly, SameSite=Lax cookie and
  calls the API through a same-origin proxy. Every mutating web route (the proxy,
  sign-in/registration, sign-out, workspace switching) requires the
  `x-probeatlas-csrf: 1` header, which a cross-site page cannot set without a
  CORS preflight that is never allowed. The proxy forwards only `/v1` paths made of
  plain URL-safe segments, so encoded traversal cannot reach other API routes.

## Tenant isolation

Every `/v1` endpoint resolves a `Principal` with exactly one workspace. Session
callers must name the workspace (`X-Workspace-Id`) and be a member; API keys are
fixed to theirs. Rows from other workspaces return 404 (not 403) to avoid
confirming that they exist. Agents never send tenant identifiers: ingestion takes
tenant, monitor, region, and target from the job row the agent was assigned.
`tests/test_tenancy.py` covers cross-tenant reads, writes, and deletes.

## Agents

Agents enroll with an enrollment token (single-use by default, optionally
region-restricted and expiring) and receive their own token. Disabled agents are
rejected on every call. Agents can only submit results for jobs they currently
hold; wrong-agent, unknown, expired, cancelled, mismatched-type, and
implausibly-timestamped results are rejected and counted. Error messages from
agents are stripped of control characters, secrets are redacted, and length is
capped.

Agent containers run as a non-root user with all Linux capabilities dropped, a
read-only root filesystem, and `no-new-privileges`. ICMP uses unprivileged ping
sockets (sysctl `net.ipv4.ping_group_range`), so `CAP_NET_RAW` is not needed.

## Logging, audit, and secrets

* Structured JSON logs; a redaction filter removes bearer tokens and ProbeAtlas
  secret formats from messages, tracebacks, and structured fields. SQL errors
  never include bound parameters (`hide_parameters=True`). Request logs contain
  method, path, status, and duration only.
* `audit_events` records registration, logins and failed logins, API key
  creation, rotation, and revocation, member changes, monitor and alert rule
  changes, webhook changes, and agent enrollment, rejection, and disabling.
* Webhook signing secrets are stored in plaintext because they are needed to
  compute HMACs. Encrypting them with a KMS key is on the production checklist.

## Rate limits

Token buckets: per workspace for `/v1` (default 300/min), per client IP for
login and registration (20/min) and agent enrollment (10/min), and per agent
for the agent protocol. Limits are per API process.

The client IP comes from `X-Forwarded-For` only when the TCP peer is listed in
`PROBEATLAS_TRUSTED_PROXIES` (the web servers and the API load balancer); from
anyone else the header is ignored. The web server forwards the browser's IP only
when `PROBEATLAS_WEB_TRUSTED_PROXY_HOPS` says how many proxies in front of it to
trust. See [operations.md](operations.md#client-ip-forwarding).

Known findings, the threat model, and the penetration test scope are tracked in
[security-review.md](security-review.md).
