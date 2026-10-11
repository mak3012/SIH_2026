# Overview

**eRTMAC-NWIS (Well Intelligence)** is our solution to Smart India Hackathon 2026 problem
statement **SIH26121** (Oil India Limited): a Nearby Wells Intelligence System, an AI-powered
offset-well knowledge and decision-support platform that runs beside eRTMAC. It turns drilling
records and real-time rig data into facts, alerts and cited answers. How each point of the
statement is met: [README → Problem statement](https://github.com/mak3012/SIH_2026#problem-statement-sih26121--what-we-built).

- **Scanned reports** (WCR, DDR) become structured facts with provenance, then searchable memory.
- **Real-time data** (eRTMAC, WITSML) is checked by deterministic rules that raise alerts.
- **Questions** in plain language get answers that cite the facts and documents they rest on.

## Repository layout

| Path | Contents |
|---|---|
| `apps/api` | FastAPI service, Alembic migrations, demo seed |
| `apps/web` | React UI (Vite): eleven screens, see [Screens and user guides](ui/screens.md) |
| `pipelines` | Dagster ingestion and processing |
| `packages/core` | Shared Python: schemas, depth maths, units |
| `packages/ui` | Shared React components and design tokens |
| `infra` | Compose support files: TLS certificates (`tls/`), `.env` secrets (`env.py`), proxy (Caddy), Postgres (TLS-only `pg_hba`, pgBackRest), Keycloak realm, Prometheus rules, Grafana dashboards, Tempo, host firewall, restore drill, licence/SBOM tool, deployment policy tests |
| `data` | Public-data downloaders, synthetic Upper Assam generator, gold set, object-store + Postgres registration (`data/raw` is git-ignored) |
| `docs` | This site |
| `tests/e2e` | Playwright: UI flows and states against seed data captured from the API (`fixtures/`), screenshots, smoke tests against a running stack |

## Quick start

```bash
make secrets # .env with generated passwords
make up      # build + start (TLS everywhere), wait until healthy, copy the CA to .certs/
make seed    # generate synthetic well data into data/raw/synthetic
make docs    # serve this site on http://localhost:8001
```

Trust `.certs/ca.crt`, then open <https://localhost:5173>. For the SIH demo, one command does
everything (stack, synthetic data, detectors, the SA-06 replay, the Live Console): `make demo`,
then follow the [five-minute script](demo.md); judges' questions: [FAQ](faq.md). See [Operations](operations/index.md)
for deployment shapes, ports, every make target and the runbooks, [Security](security.md) for
roles, network, TLS, audit and the threat model, and [Rig Edge Pack](operations/edge.md) for the
offline rig box.
