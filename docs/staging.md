# Staging deployment: AWS control plane, agents on Azure, AWS, GCP, and OVHcloud

This runbook stands up the staging environment: one control plane and four measurement agents, each on a
different provider in a different part of the world. Everything lives in
[`deploy/terraform/staging`](../deploy/terraform/staging), [`deploy/staging`](../deploy/staging) (control-plane
host), and [`deploy/agent`](../deploy/agent) (agent host).

| Role | Provider and region | Region label | Size (default) |
| --- | --- | --- | --- |
| Control plane: Caddy, web, API, worker | AWS eu-central-1 (Frankfurt), EC2 + RDS PostgreSQL 16 | — | `t3.small`, `db.t4g.micro` |
| Agent | Microsoft Azure westeurope (Netherlands) | `eu-west` | `Standard_B2ats_v2` |
| Agent | AWS us-east-1 (N. Virginia) | `us-east` | `t3.micro` |
| Agent | Google Cloud asia-southeast1 (Singapore) | `ap-southeast` | `e2-small` |
| Agent | OVHcloud Public Cloud SYD1 (Sydney) | `au-east` | `d2-2` |

Estimated cost: about $95–105 per month on demand (November 2026 price trackers; check each provider's
calculator). Each region has one agent, so every provider appears separately in regional comparisons.
Location fields follow the provider's documented region and are operator assertions.

## How it fits together

```
browsers, API clients, agents ──HTTPS──▶ Caddy :443 ─┬─ app.<domain> ─▶ web :3000 ──▶ api :8000
                                                      └─ api.<domain> ─▶ api :8000
                                     api, worker ──TLS──▶ RDS PostgreSQL (private subnets, control plane only)
```

* **Images:** CI publishes `ghcr.io/wronski-tech/probeatlas-{server,agent,web}` on every push to `main`,
  tagged with the full commit SHA and `main`. A deployment is always one commit SHA.
* **Control-plane host:** cloud-init installs Docker and the AWS CLI and writes:
  * `/etc/probeatlas/settings.env`: non-secret settings, with `PROBEATLAS_ENV=production` and invite-only
    sign-up;
  * `/etc/probeatlas/host.env`: where the secrets and backups live;
  * the host scripts.

  It then runs `probeatlas-deploy <sha>`.
* **Secrets:** the database URL exists only in SSM Parameter Store (SecureString) and in
  `/etc/probeatlas/control-plane.env` (0600), which `probeatlas-render-env` writes at deploy time using the
  instance role. The role can read that one parameter and write dumps, nothing else.
* **No metadata access from containers:** instance metadata requires IMDSv2 with hop limit 1, so containers
  cannot borrow the host role.
* **No public internal ports:** the API (8000) is not published. Metrics listen on `127.0.0.1:9100`
  (worker) and `127.0.0.1:9101` (API). The database has no internet route and accepts connections only from
  the control plane's security group.
* **Agents:** cloud-init installs Docker and runs `probeatlas-agent-run`, which starts the agent container
  with `--cap-drop ALL --read-only`, ICMP via `ping_group_range`, and a state volume.
  * Agents need outbound HTTPS only. They get no cloud API permissions.
  * Inbound SSH is allowed only from `operator_cidrs`, or not at all.

## Prerequisites

1. **Accounts and credentials** on the operator machine. Never commit them, and never paste them into
   chats or tickets.
   * **AWS:** a profile or role allowed to manage EC2, VPC, RDS, S3, SSM, and IAM.
   * **Azure:** `az login`, or a service principal (`ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID`),
     plus the subscription ID.
   * **Google Cloud:** `gcloud auth application-default login`, or a service account key
     (`GOOGLE_APPLICATION_CREDENTIALS`), with the Compute Engine API enabled in the project.
   * **OVHcloud:** a Public Cloud project with the SYD1 region enabled. Create an OpenStack user, download
     its openrc file, and `source` it to get the `OS_*` variables.
2. **A domain** where you can create two A records (for example `app.staging.example.com` and
   `api.staging.example.com`).
3. **Terraform ≥ 1.10.**
4. **A private, versioned S3 bucket for Terraform state.** State contains the database password and the
   enrollment tokens.
5. **Public GHCR images.** CI publishes them on every green push to `main`. They are linked to the public
   repository and pull without credentials. Check with `docker pull ghcr.io/wronski-tech/probeatlas-agent:main`
   on a machine that is not logged in to GHCR.
   - If you add a new image, GHCR can answer `denied` to the very first push that creates its package.
     Re-run the job: the package then exists and accepts the push. That is what happened when these three
     packages were first published.

## Phase 1: control plane

```bash
cd deploy/terraform/staging
cp backend.hcl.example backend.hcl            # fill in the state bucket
cp terraform.tfvars.example terraform.tfvars  # domains, image_tag (a main commit SHA), SSH key, Azure/GCP ids, contact_email
terraform init -backend-config=backend.hcl
git add .terraform.lock.hcl                   # commit the provider lock file from the first init
terraform apply
```

With no `agent_enrollment_tokens`, this creates only the AWS control plane. Then:

1. Create the DNS A records for `app_domain` and `api_domain` pointing at the `control_plane_ip` output.
   Caddy obtains certificates as soon as the names resolve; it retries on its own.
2. Wait about 5 minutes for the first boot, then check:
   `curl https://<api_domain>/readyz` should return `{"status":"ready"}`, and `https://<app_domain>` should
   show the landing page. Its contact address is `contact_email` (default: `acme_email`); agents send
   `https://<app_domain>/probes` in their User-Agent automatically.
3. Open a shell on the host without SSH:
   `aws ssm start-session --target $(terraform output -raw control_plane_instance_id)`, or SSH from
   `operator_cidrs`. Useful commands, run in `/opt/probeatlas` with `sudo`:

   ```bash
   docker compose ps
   docker compose logs -f api worker
   docker compose exec api probeatlas-admin create-workspace --name "ProbeAtlas ops" --owner-email you@example.com
   ```

   Open the printed `/invite/pai_…` path on `https://<app_domain>` to create your account. The link is
   single-use and expires in 7 days.

## Phase 2: agents

On the control-plane host, issue one single-use, region-bound token per agent (each valid for 24 h):

```bash
for r in eu-west us-east ap-southeast au-east; do
  docker compose exec -T api probeatlas-admin create-enrollment-token --region "$r" --max-uses 1 \
    --expires-in-hours 24 --description "staging agent $r"
done
```

Pass them to Terraform through the environment rather than a file, then apply:

```bash
export TF_VAR_agent_enrollment_tokens='{"eu-west":"pae_…","us-east":"pae_…","ap-southeast":"pae_…","au-east":"pae_…"}'
terraform apply
```

Within a few minutes, **Agents** in the dashboard (or `probeatlas-admin list-agents`) shows four `online`
agents with the expected labels and capabilities, including `icmp`.

- **Tokens:** they appear in cloud-init user data, which provider consoles can display. That is acceptable
  only because each token is single-use and expires. Once enrolled, an agent uses its own credential stored
  in its volume.
- **Changing tokens later** does not recreate agents: Terraform ignores user-data changes after creation.
- **Re-enrolling an agent** (for example after `disable-agent`): issue a new token, update
  `/etc/probeatlas/agent.env` on that host, delete the `probeatlas-agent` volume, and run
  `probeatlas-agent-run`.

## Deploying a new version

Wait until CI on `main` is green for the commit (images are published only then). Then:

```bash
# control plane: fetches that commit's compose.yml/Caddyfile/pgbackup.sh, refreshes settings, runs
# migrations, and waits for every service (including Caddy) to be healthy
sudo probeatlas-deploy <full commit SHA>

# each agent (SSH, Session Manager, or the provider console): graceful stop, results are flushed
sudo probeatlas-agent-run <full commit SHA>
```

- **During an API restart,** agents buffer results (up to 2000 each) and retry, so short deploys lose no
  measurements.
- **Rolling back** is deploying an older SHA, as long as no migration since then needs reverting. Check
  `git log -- src/probeatlas/server/migrations` before rolling back.

## Backups and restore drills

- **RDS automated backups:** 7 days of point-in-time recovery.
- **Daily logical dump:** `probeatlas-backup.timer` runs at 03:15 UTC. It writes a verified dump to
  `/var/backups/probeatlas` (kept 7 days) and uploads it to `s3://<backup_bucket>/daily/`. The bucket is in
  eu-west-1, versioned, and expires dumps after 35 days.
- **Monthly drill:** `sudo probeatlas-backup drill` takes a fresh dump, restores it into a scratch database
  on the same RDS instance, compares row counts, and drops it.
- **Last run:** `systemctl status probeatlas-backup.service` and `journalctl -u probeatlas-backup`.
- **Full recovery:** see [backup-restore.md](backup-restore.md#restoring-production).

## Validation checklist (before inviting a design partner)

- [ ] All four agents `online` with correct region, country, city, provider, and capabilities.
- [ ] A monitor on `https://<api_domain>/healthz` from all four regions. Latency should follow geography:
      Netherlands < N. Virginia < Singapore and Sydney.
- [ ] HTTP, DNS, TCP, and ICMP monitors against a well-known public endpoint succeed from every region.
- [ ] Stop one agent (`docker stop probeatlas-agent`):
  - it shows `offline` within 60 s;
  - its region's results show as **missing**;
  - the other regions are unaffected;
  - `docker start` recovers it without re-enrolling.
- [ ] A target that fails from one region only opens a regional incident, delivers a signed webhook, and
      resolves after recovery.
- [ ] Deploy a new SHA while checks run; no gap in results.
- [ ] `sudo probeatlas-backup drill` passes, and a dump is in S3.
- [ ] Security spot checks from outside:
  - only 80/443 answer on the control plane;
  - `https://<api_domain>/metrics` returns 404;
  - `/register` without an invitation is refused;
  - agents have no open ports except SSH from `operator_cidrs`.
- [ ] Soak for 7 days. Watch `docker compose logs worker` for scheduler lag and failed notifications.

## Teardown

```bash
terraform apply -var db_deletion_protection=false   # allow deleting the database (a final snapshot is kept)
terraform destroy
```

The S3 backup bucket must be emptied first if it should go too. The final RDS snapshot and the state bucket
stay until you delete them by hand.

## Known gaps

These are fine for staging and are tracked in [operations.md](operations.md#production-readiness-checklist-not-yet-done).

- **No paging:** nothing pages anyone when the worker stalls. Self-monitoring covers the API; add an
  external uptime check and a scheduler-lag alert before production.
- **Single points of failure:** one control-plane host and a single-AZ database.
- **Rate limits are per process,** which is correct with one API container.
- **Webhook signing secrets** are stored in plaintext in the database.
