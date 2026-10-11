# Operations

How to run the stack: where it runs, the make targets, ports, certificates, secrets, the
service levels and what watches them, backups, and the audit anchor. When something goes wrong,
start from the [runbooks](#runbooks). The Rig Edge Pack has its own page: [Rig Edge Pack](edge.md).

## Deployment shapes

One compose file (`docker-compose.yml`) runs everywhere. `.env` picks the files and profiles.

| Where | `COMPOSE_FILE` | `COMPOSE_PROFILES` | Notes |
|---|---|---|---|
| Laptop | `docker-compose.yml,docker-compose.dev.yml` (the `.env.example` default) | `hq,dev` | Dev overlay: host ports on 127.0.0.1 for DB tools, S3, Dagster, Prometheus and the Keycloak admin console; internet for model pulls and `make seed` downloads; Ollama |
| Headquarters | unset (base file only) | `hq` | Ingestion (Dagster) and observability. No service can reach the internet |
| Rig box | unset | `edge` | One machine: app, real-time service, local LLM, sync agent. See [Rig Edge Pack](edge.md) |
| Demo of the live path | add `realtime` | | WITSML simulator + real-time service (`make realtime`) |

Every service is on the `backend` network, which is `internal`: no route out and no published
ports. Only `proxy` (Caddy) publishes ports. Only `proxy`, `realtime` (the rig's WITSML store)
and `edge-sync` (headquarters) sit on the `edge` network, which has a route out; on a Linux host,
`infra/firewall/no-egress.sh` limits it to the destinations you name:

```bash
sudo infra/firewall/no-egress.sh
```

```bash
sudo infra/firewall/no-egress.sh 10.20.0.15:443 10.1.2.3:443
```

The first form (headquarters) allows no outbound connections at all; the second (rig box) allows
the rig's WITSML store and headquarters only. Docker Desktop has no host firewall: there the
`internal` networks are the control. Details and the reasons: [Security](../security.md#network).

## First start

```bash
make secrets
```

```bash
make up
```

`make secrets` writes `.env` from `.env.example` with every secret generated (32 random
characters) and keeps whatever is already set. `make up` builds, starts, waits for health and
copies the stack's CA certificate to `.certs/ca.crt`. Trust that certificate in the browser or
OS once (on Windows: `.\demo.ps1 trust`), then open <https://localhost:5173>. Dev sign-in users:
[Security → Dev users](../security.md#dev-users).

## Make targets

| Target | Does |
|---|---|
| `make secrets` | Create or update `.env` from `.env.example`, generating every empty secret |
| `make up` | Build, start the services `.env` selects, wait for health, then `make ca` |
| `make ca` | Copy the stack's CA certificate to `.certs/ca.crt` (trust it; host tools verify the database with it) |
| `make reset-auth` | Re-import `infra/keycloak/realm.json` into an empty Keycloak database (Keycloak imports only into an empty one); deletes accounts created by sign-up |
| `make down` | Stop the stack (volumes kept; `docker compose down -v` wipes them) |
| `make migrate` | `alembic upgrade head` in the api container (it also runs on every api start) |
| `make seed` | Demo wells into Postgres; then, in the `datatools` container: download Sodir + FORGE (+ Volve if configured), generate 30 synthetic wells with PDFs, voice notes and the gold set, register every document in the object store + Postgres. Downloads need the dev overlay (internet). See [Data](../data.md#make-seed) |
| `make seed-offline` | Same without the public-data downloads (works with no egress) |
| `make models` | Pull `LLM_MODEL`, `VLM_MODEL` and `EMBED_MODEL` into the dev Ollama |
| `make rt-models` | Train the real-time detectors on synthetic wells (group K-fold by well), evaluate on held-out wells, write the metrics into [Real-time](../realtime.md#results); file `data/models/realtime/detectors.joblib` |
| `make realtime` | Start the `realtime` profile: WITSML simulator (over TLS) replaying `RT_WELLBORE_ID` at `RT_SIM_SPEED`, and the real-time service polling it. Live view `#/live/<id>`. Run `make seed` first |
| `make assistant-eval` | Load the synthetic truth as accepted facts, embed passages, run the 100-question assistant evaluation (rules, noisy drafter, LLM) and write the results into [Assistant](../assistant.md#evaluation) |
| `make replay WELL=SA-06 SPEED=300` | One synthetic well through the live path (WITSML simulator → poller → real-time service) with leave-one-well-out priors, at SPEED x (1–600; 0 = as fast as possible) |
| `make evaluation` | Leave-one-well-out replay of every synthetic well, extraction P/R from the last `make eval`, assistant accuracy (rules + LLM): writes `docs/eval-report/` and the generated sections of [Evaluation](../evaluation.md) |
| `make ui-fixtures` | After `make evaluation`: capture the UI tests' seed data from the API into `tests/e2e/fixtures` |
| `make screenshots` | Write `docs/ui/screenshots/*.png` from those fixtures ([Screens](../ui/screens.md)) |
| `make eval` | Run extraction on the gold set and rewrite the results in [Evaluation](../evaluation.md) |
| `make backup [TYPE=full\|diff\|incr]` | pgBackRest backup (creates the stanza the first time), then `check` |
| `make backup-info` | Backups in the repository, with sizes and WAL range |
| `make restore-drill` | Restore the latest backup into a throwaway container and check it ([runbook](runbooks/db-restore.md)) |
| `make audit-anchor` | Append the audit chain's head to `audit-anchors.jsonl` (copy it off the box) |
| `make audit-verify` | Verify the chain and every anchor; exit 1 on a break or truncation |
| `make licences` | Licence policy check; rewrite the generated table in [Licences](../licences.md) |
| `make sbom` | CycloneDX SBOM of the Python and npm dependencies (`sbom.cdx.json`) |
| `make edge-package WELL=<id>` | Hazard package for a rig box ([Rig Edge Pack](edge.md)) |
| `make test` | Python unit tests (database tests skip) and the deployment policy tests (`infra/tests`) |
| `make test-db` | All Python tests, including database tests, against a throwaway `test` database in the running stack |
| `make lint` | Ruff lint + format check |
| `make typecheck` | mypy + tsc |
| `make docs` | Serve this site on <http://localhost:8001> |
| `make e2e` | Playwright tests (UI tests mock the API from the captured fixtures; the API health test needs the stack) |
| `make demo` | The SIH demo: stack with a small local LLM, synthetic data, facts and index, detectors (once), SA-06 replayed at 300× around its Barail loss with explanations, Live Console opened ([Demo](../demo.md)) |
| `make demo-replay [DEMO_WELL=] [DEMO_FOCUS=loss\|kick\|stuck_pipe] [SPEED=]` | Replay a held-out well from 20 min before its first recorded event of that kind to 10 min after, in the `apitools` container (`docker logs -f wells-demo-replay`) |
| `make demo-open` | Open the Live Console of `DEMO_WELL` |
| `make demo-llm-down` / `make demo-llm-up` | Stop the LLM and replay at 600× (demo step 6) / start it again |
| `make demo-down` | Stop the replay and the stack (data kept) |
| `make urls` | Every address and where its password is |

On Windows without make, `.\demo.ps1 up | seed | facts | realtime | replay | urls | down` does
the same for the demo path.

## Services and ports

Public (through `proxy`, HTTPS, bound to `BIND_ADDR`):

| Endpoint | Port | Upstream |
|---|---|---|
| Web app, path `/` | 5173 | `web:8443` (nginx, TLS) |
| API (REST, SSE, WebSocket), path `/api` | 5173 | `api:8000` (uvicorn `--root-path /api`, TLS) |
| Keycloak sign-in and sign-up, path `/auth` | 5173 | `keycloak:8443` (TLS); `/auth/admin` answers 404 |
| Grafana | 3000 | `grafana:3000` (TLS) |

Laptop only (dev overlay, 127.0.0.1): Postgres 15432 (TLS required), S3 9000 (TLS),
Keycloak admin console 8444 (`/auth/admin`), Dagster 3001, Prometheus 9090, Ollama 11434.

| Service | Profile | Health check |
|---|---|---|
| init | always (one-shot) | completes: CA and certificates in the `certs` volume |
| postgres | always | `pg_isready` over TCP |
| s3 | always | SeaweedFS master status + S3 over TLS |
| keycloak | always | `/health/ready` on management port 9000 |
| api | always | `GET https://localhost:5173/api/health` |
| web | always | `GET https://127.0.0.1:8443/` |
| proxy | always | `GET https://127.0.0.1:5173/` |
| dagster | hq | `GET /server_info` (loopback) |
| prometheus | hq, edge | `/-/healthy` |
| grafana | hq, edge | `/api/health` over TLS |
| tempo | hq | – |
| loki | hq | `/ready` |
| witsml-sim | realtime | `GET https://localhost:8090/health` |
| apitools | tools (one-shot) | – (api image with `data/` mounted: truth loader, detector training, demo replay) |
| realtime | realtime, edge | – (state in `rt_feed`; metrics on 9464) |
| llm | edge | `GET /health` |
| edge-sync | edge | – (logs each sync) |
| ollama | dev (laptop overlay) | `ollama list` |

## Certificates

The `init` job (`infra/tls/certs.py`) creates a private CA (key in the `ca` volume, which only
`init` mounts) and one certificate per service in the `certs` volume. Each service mounts only
its own folder, read-only; clients mount `client/`, which holds the CA certificate alone.
Certificates last 397 days; `init` runs on every `up` and re-issues any that expire within 30
days or whose names changed. Add host names browsers use to `TLS_EXTRA_SANS`.

To use the organisation's PKI instead: put its signed `tls.crt` / `tls.key` in each service's
folder of the `certs` volume and its root in `ca.crt`; `init` leaves valid certificates alone.

## Secrets

All secrets are environment variables, never in images or the repository: `POSTGRES_PASSWORD`,
`S3_ROOT_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`,
`BACKUP_CIPHER_PASS` (encrypts the backup repository; without it backups cannot be read) and
`EDGE_SYNC_CLIENT_SECRET`. Compose refuses to start when one is missing. Laptops use the
generated `.env`; headquarters and rig boxes inject them from the secret store (for example
Vault, or a SOPS-encrypted env file decrypted at deploy) and keep a copy of
`BACKUP_CIPHER_PASS` outside the box. Rotation: [Security → Secrets](../security.md#secrets).

## Observability

**Traces** (OpenTelemetry, OTLP over HTTPS to Tempo, viewed in Grafana → Explore → Tempo):

| Path | Spans |
|---|---|
| Ingestion | `ingest.<asset>` for every Dagster step of a document or voice note; all steps of one document's run share one trace (trace id from run id + partition), with `llm.call` children |
| Alert path | `rt.sample` per sample (5 % sampled), with an `alert.raise` event when one is raised; `rt.explain` per explanation with its `llm.call` |
| Chat | the HTTP request (FastAPI), `assistant.ask` (status, intent, drafter, citations), `llm.call` per attempt |

**Metrics** (Prometheus; `infra/prometheus/rules.yml` holds the alerts, each naming its runbook):

| Metric | What | Objective / alert |
|---|---|---|
| `wells_feed_latency_seconds` | Sample timestamp (rig clock) to stored and published | p95 < 10 s (`FeedLatencyHigh`); no samples for 2 min (`FeedSilent`) |
| `wells_alert_evaluation_seconds` | Sample received to rules evaluated, alert stored and delivered | p99 < 5 s (`AlertEvaluationSlow`) |
| `wells_llm_seconds{prompt,model,outcome}` | One LLM call attempt | p95 < 60 s (`LLMSlow`); > 50 % errors for 10 min (`LLMDown`) |
| `wells_verifier_rejects_total{component}` | LLM drafts refused by rule verification (assistant, alert explainer, briefing) | > 20 in 1 h (`VerifierRejectsHigh`) |
| `wells_queue_depth{queue}` | review (values for a data steward), extraction (documents not read yet), transcription, explanation (alerts waiting for the LLM) | review > 500 for 1 h; explanation > 10 for 15 min |

Grafana's **Service levels** dashboard (`infra/grafana/dashboards/slo.json`) shows each
objective with its threshold, LLM calls by outcome, verifier rejects, queue depth, scrape
targets, and slow traces (TraceQL: `rt.sample`, `assistant.ask`, `ingest.*`, `llm.call` over
2 s). **Platform health** shows scrape targets and API memory.

Clock skew on the rig inflates feed latency: keep the rig's WITSML server and this host on NTP
(see the [stale-feed runbook](runbooks/stale-feed.md)).

## Backups

PostgreSQL archives every WAL segment to pgBackRest (`archive_command`, at most every 5 min via
`archive_timeout`) in the `pgbackrest` volume, AES-256 encrypted with `BACKUP_CIPHER_PASS`.
Retention: 4 full backups, 7 differentials. Schedule on the host (cron):

```cron
15 1 * * 0   cd /opt/wells && make backup TYPE=full
15 1 * * 1-6 cd /opt/wells && make backup TYPE=diff
30 2 * * 1   cd /opt/wells && make restore-drill
5 * * * *    cd /opt/wells && make audit-anchor && cp audit-anchors.jsonl /mnt/worm/
```

Production adds an off-site repository (`repo2`, S3 or a second host) in
`infra/postgres/pgbackrest.conf`. Recovery point: at most 5 minutes of committed data (the
archive timeout). The weekly drill measures the recovery time and appends it to
`restore-drills.jsonl`; CI runs one on every push.

## Audit anchor

`audit_log` is hash-chained inside the database ([Security → Audit](../security.md#audit)).
Deleting the newest entries would leave a valid but shorter chain, so the head is written out
hourly (`make audit-anchor`) to a file kept off the box (write-once storage, or mailed to the
security team). `make audit-verify` checks the chain and that every anchored entry is still
there with the same hash.

## Runbooks

| Runbook | When |
|---|---|
| [Stale feed](runbooks/stale-feed.md) | Live view grey, `FeedSilent`, `FeedLatencyHigh`, `AlertEvaluationSlow` |
| [LLM down](runbooks/llm-down.md) | `LLMDown`, `LLMSlow`, `ExplanationQueueStuck`; answers say "rules only" |
| [Database restore](runbooks/db-restore.md) | Lost or corrupted database, bad migration, restore drill failed |
| [Model rollback](runbooks/model-rollback.md) | A detector bank or LLM change made alerts or answers worse; `VerifierRejectsHigh` |
