# Backups and restore

PostgreSQL is the only stateful component: configuration, jobs, raw results, incidents, audit events, and
agent identities all live there. Agents keep only their own credentials (re-issued by enrolling again), and
the API, worker, and web containers are stateless.

## Policy (recommended for production)

| Layer | What | Target |
| --- | --- | --- |
| Managed database PITR | Continuous WAL archiving by the provider (RDS, Cloud SQL, Neon, Crunchy, …) | 7–14 days point-in-time recovery; RPO ≈ minutes |
| Logical dumps | `deploy/backup/pgbackup.sh backup` daily, copied to object storage in another account/region with versioning and an expiry rule | 30 daily + 12 monthly |
| Restore drill | `deploy/backup/pgbackup.sh drill` (or a PITR restore) at least monthly and after schema changes | Restore succeeds, row counts match, `alembic_version` is current |

Recovery objectives for the MVP: RPO ≤ 24 h from dumps alone, minutes with PITR; RTO ≤ 1 h (restore,
point `PROBEATLAS_DATABASE_URL` at the restored database, restart `api` and `worker`).

Dumps contain password hashes, token hashes, and **plaintext webhook signing secrets**. Store them
encrypted (bucket-level KMS), restrict access to operators, and never attach them to tickets.

## The script

`deploy/backup/pgbackup.sh` wraps `pg_dump`/`pg_restore`/`psql`:

```bash
deploy/backup/pgbackup.sh backup [DIR]            # DIR/probeatlas-<UTC>.dump (custom format, verified)
deploy/backup/pgbackup.sh restore FILE [DBNAME]   # into a NEW database (default probeatlas_restore)
deploy/backup/pgbackup.sh drill [DIR]             # backup → restore to scratch DB → compare row counts → drop
```

* With `PGURL=postgresql://user:pass@host:5432/probeatlas?sslmode=require` it uses local client tools
  (major version ≥ the server's). Without `PGURL` it runs the tools inside the Compose `db` service.
* Dumps are written with `umask 077`, verified with `pg_restore --list`, and only then renamed from
  `*.partial` to `*.dump`.
* `restore` refuses to overwrite an existing database.
* Make targets: `make backup`, `make restore FILE=backups/x.dump [DB=name]`, `make restore-drill`
  (set `COMPOSE="sudo docker compose"` in orbs).

Example scheduled backup (cron on an operator host or a scheduled container job):

```bash
PGURL=... deploy/backup/pgbackup.sh backup /var/backups/probeatlas \
  | xargs -I{} aws s3 cp {} s3://probeatlas-backups/daily/ --sse aws:kms
```

## Restoring production

1. Stop writers: scale `worker` to 0 and put the API in maintenance (or stop it). Agents buffer up to
   2000 results each and retry, so short outages lose no measurements.
2. Restore into a new database: `PGURL=... deploy/backup/pgbackup.sh restore FILE probeatlas_restored`
   (or create a PITR clone with the provider).
3. Run `probeatlas-admin migrate` against it (no-op if the dump is from the current version).
4. Point `PROBEATLAS_DATABASE_URL` at the restored database and start `api`, then `worker`.
5. Check `/readyz`, `/v1/agents` (agents reconnect on their next heartbeat), and that new results arrive.

Jobs that were pending or claimed at dump time expire as `unclaimed`/`lease_timeout` and show as missing
data for that window; they are not replayed. Results submitted after the dump and before the restore
are lost unless PITR was used.

## Verified drill (local)

Run against the Compose stack on 2026-10-27 (both through `docker compose exec` and with `PGURL` from a
`postgres:16` client container):

```
schema revision: 0001
  users 1 · workspaces 1 · memberships 1 · api_keys 3 · monitors 6 · alert_rules 1 · incidents 3
  incident_events 8 · notification_channels 0 · agents 2 · jobs 3154 · results 3131 · audit_events 16
drill passed: …/probeatlas-20261027T163156Z.dump restores and every table matches the source row count
```

On a live system the source counts are taken after the dump, so tables that receive writes (`jobs`,
`results`) can be slightly larger in the source; the drill prints the difference instead of failing.
