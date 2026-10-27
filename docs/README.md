# ProbeAtlas documentation

| If you want to… | Read |
| --- | --- |
| Run the product locally and try the demo | [../README.md](../README.md) |
| Understand the processes, data flow, data model, and trade-offs | [architecture.md](architecture.md) |
| Call the API or use the CLI | [api.md](api.md) · [openapi.json](openapi.json) · `/docs` on a running API |
| Run agents, read metrics, understand recovery and retention, deploy | [operations.md](operations.md) |
| Back up, restore, and run restore drills | [backup-restore.md](backup-restore.md) |
| Understand the security controls | [security.md](security.md) |
| See the threat model, review findings, and pen-test scope | [security-review.md](security-review.md) |
| Plan the Terraform provider | [terraform.md](terraform.md) |
| Contribute code (humans) | [../CONTRIBUTING.md](../CONTRIBUTING.md) |
| Change code as an AI agent (invariants, commands, recipes) | [../AGENTS.md](../AGENTS.md) |

The code is documented in place: every Python module, class, and function has a docstring, and every
TypeScript function and class in `web/src` has a JSDoc comment. CI enforces this with ruff (`D` rules) and
`scripts/check_docstrings.py`. The best entry points for reading the code:

| Topic | Start here |
| --- | --- |
| Wire contract between agents and the control plane | `src/probeatlas/common/schemas.py` |
| Outbound network policy (SSRF) | `src/probeatlas/common/netpolicy.py`, `src/probeatlas/common/safehttp.py` |
| Agent loop | `src/probeatlas/agent/runner.py` |
| Authentication and tenant resolution | `src/probeatlas/server/deps.py` |
| Scheduling and job leases | `src/probeatlas/server/scheduling.py` |
| Result ingestion | `src/probeatlas/server/ingestion.py` |
| Alerting and incidents | `src/probeatlas/server/alerting.py` |
| Aggregation and regional comparison | `src/probeatlas/server/stats.py` |
| Dashboard data access | `web/src/lib/api.ts`, `web/src/app/api/proxy/[...path]/route.ts` |
