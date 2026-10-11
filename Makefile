# Files and profiles come from .env (COMPOSE_FILE, COMPOSE_PROFILES): a laptop gets the dev
# overlay and the hq + dev profiles; headquarters and rig boxes set their own (docs/operations).
COMPOSE := docker compose
PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python)

.PHONY: up down secrets ca migrate models seed seed-offline eval rt-models realtime assistant-eval replay evaluation ui-fixtures screenshots test test-db lint typecheck docs demo e2e backup backup-info restore-drill audit-anchor audit-verify licences sbom edge-package demo-replay demo-open demo-llm-down demo-llm-up demo-down urls reset-auth public-wells

# .env from .env.example with every secret generated; reruns keep what is set.
secrets:
	$(PYTHON) infra/env.py

.env:
	$(PYTHON) infra/env.py

up: .env
	$(COMPOSE) up -d --build --wait --wait-timeout 900
	@$(MAKE) --no-print-directory ca

# The stack's CA certificate on the host (.certs/ca.crt): trust it in the browser / OS, and host
# tools (HOST_ENV) verify the database against it.
ca:
	@mkdir -p .certs
	$(COMPOSE) run --rm --no-deps -T --entrypoint cat init /certs/ca.crt > .certs/ca.crt
	@echo "CA certificate: .certs/ca.crt"

# Sync the public wells now (Sodir FactPages; Dagster also runs it daily at 05:00 UTC). Needs
# internet; the rest of the demo does not.
public-wells:
	$(COMPOSE) exec -T dagster dagster job execute -m pipelines -j public_wells_job

# Re-import infra/keycloak/realm.json (Keycloak imports it only into an empty database).
# Deletes users who signed up.
reset-auth:
	$(COMPOSE) stop keycloak
	. ./.env && $(COMPOSE) exec -T postgres psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" \n	  -c 'DROP DATABASE IF EXISTS keycloak WITH (FORCE)' -c 'CREATE DATABASE keycloak'
	$(COMPOSE) up -d --wait keycloak proxy

down:
	$(COMPOSE) down

# The api container also migrates on every start; this is for running it by hand.
migrate:
	$(COMPOSE) exec api alembic -c apps/api/alembic.ini upgrade head

# Pull the LLM and VLM the pipelines use into the dev Ollama (names from .env).
models: .env
	. ./.env && $(COMPOSE) exec ollama ollama pull $$LLM_MODEL && $(COMPOSE) exec ollama ollama pull $$VLM_MODEL \
	  && $(COMPOSE) exec ollama ollama pull $$EMBED_MODEL

# Extraction evaluation on the gold set; writes docs/evaluation.md. OCR strata need the extras:
# uv sync --all-packages --extra ocr --extra speech
eval:
	uv run python -m pipelines.evaluate --write-docs

# Demo wells (api.seed), then data/seed.py in the datatools container: Sodir + FORGE (+ Volve if
# configured) downloads, 30 synthetic Upper Assam wells with PDFs, voice notes and the gold set,
# and registration of every document in the object store + Postgres. Idempotent; reruns reuse what exists.
seed: .env
	$(COMPOSE) exec api python -m api.seed
	$(COMPOSE) run --rm --build datatools python seed.py

# Same, without network downloads (synthetic data only; fonts must already be cached).
seed-offline: .env
	$(COMPOSE) exec api python -m api.seed
	$(COMPOSE) run --rm --build datatools python seed.py --offline

# Real-time detectors: train on synthetic wells (group K-fold by well), evaluate on held-out
# wells, write the metrics into docs/realtime.md. Writes data/models/realtime/detectors.joblib.
rt-models:
	uv run python -m api.realtime train --wells 60 --hours 12 --test-wells 12 --write-docs

# Real-time demo: WITSML simulator + real-time service for RT_WELLBORE_ID (run `make seed` first).
# Live view: http://localhost:5173/#/live/<RT_WELLBORE_ID>
realtime: .env
	$(COMPOSE) --profile realtime up -d --build --wait witsml-sim realtime

# Assistant: load the synthetic truth as accepted facts, embed passages, run the 100-question
# evaluation (rules, noisy drafter, and the configured LLM) and write the results into
# docs/assistant.md. Needs DATABASE_URL (e.g. the stack's postgres on localhost).
assistant-eval: .env
	$(HOST_ENV) \
	  && (cd data && uv run python -m synthetic_assam.load_truth) \
	  && uv run python -m api.assistant index \
	  && uv run python -m api.assistant eval --modes rules noisy llm --write-docs

# Host commands against the stack: its postgres and ollama on localhost.
HOST_ENV = . ./.env && export DATABASE_URL=postgresql://$$POSTGRES_USER:$$POSTGRES_PASSWORD@localhost:$$POSTGRES_PORT/$$POSTGRES_DB \
	  LLM_BACKEND EMBED_BACKEND EMBED_MODEL LLM_MODEL LLM_URL=http://localhost:$$OLLAMA_PORT

# One synthetic well through the live path (WITSML simulator -> poller -> real-time service),
# leave-one-well-out priors, at SPEED x; watch it at http://localhost:5173/#/live/<wellbore id>.
WELL ?= SA-06
SPEED ?= 300
replay: .env
	$(HOST_ENV) && uv run python -m api.replay well --well $(WELL) --speed $(SPEED)

# Full evaluation: leave-one-well-out replay of every synthetic well (unpaced, parallel),
# extraction P/R from the last `make eval`, assistant accuracy (rules + LLM); writes
# docs/eval-report/ (HTML, PDF, metrics.json) and the generated sections of docs/evaluation.md.
evaluation: .env
	$(HOST_ENV) \
	  && (cd data && uv run python -m synthetic_assam.load_truth) \
	  && uv run python -m api.replay evaluate --chat-llm --write-docs

# Seed data for the UI tests and screenshots, captured from the API after `make evaluation`
# (writes to the database once: the review queue comes from a real extraction of 3 documents).
ui-fixtures: .env
	$(HOST_ENV) SYNTHETIC_RAW=data/raw \
	  && uv run python tests/e2e/fixtures/capture.py extract \
	  && uv run python tests/e2e/fixtures/capture.py capture

# docs/ui/screenshots from the fixtures (vite dev server started if none is running).
screenshots:
	npm run e2e -- screens.spec.ts

# Unit tests; database tests skip unless TEST_DATABASE_URL is set (see test-db).
test:
	uv run pytest

# Database tests against a throwaway database in the running stack. WIPES that database.
test-db: .env
	$(COMPOSE) exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "DROP DATABASE IF EXISTS test WITH (FORCE)" -c "CREATE DATABASE test"'
	. ./.env && PGSSLMODE=verify-full PGSSLROOTCERT=$(CURDIR)/.certs/ca.crt \
	  TEST_DATABASE_URL=postgresql://$$POSTGRES_USER:$$POSTGRES_PASSWORD@localhost:$$POSTGRES_PORT/test uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy
	npm run typecheck

docs:
	uv run mkdocs serve

e2e:
	npm run e2e

# ---------------------------------------------------------------- operations (docs/operations)
# pgBackRest: full on Sunday, differential otherwise (cron: docs/operations/index.md#backups).
TYPE ?= diff
backup:
	$(COMPOSE) exec -T postgres pgbackrest --stanza=wells stanza-create
	$(COMPOSE) exec -T postgres pgbackrest --stanza=wells --type=$(TYPE) backup
	$(COMPOSE) exec -T postgres pgbackrest --stanza=wells check

backup-info:
	$(COMPOSE) exec -T postgres pgbackrest --stanza=wells info

# Restore the latest backup into a throwaway container and check it (runbooks/db-restore.md).
restore-drill:
	sh infra/backup/restore-drill.sh

# Audit chain head to an anchor file (hourly from cron; copy the file off the box), and verify
# the chain and every anchor.
ANCHORS ?= audit-anchors.jsonl
audit-anchor:
	$(COMPOSE) exec -T api python -m api.audit anchor >> $(ANCHORS)

audit-verify:
	$(COMPOSE) exec -T api python -m api.audit verify --anchors - < $(ANCHORS)

# Licence inventory and policy check; rewrites the generated table in docs/licences.md.
licences:
	uv run python infra/licences.py --write-docs

# CycloneDX SBOM of the Python and npm dependencies (CI also scans the images).
sbom:
	uv run python infra/licences.py --sbom sbom.cdx.json

# Hazard package for a rig box (docs/operations/edge.md): WELL=<wellbore id>
edge-package:
	$(COMPOSE) exec -T api python -m api.edge pack --wellbore-id $(WELL) > hazard-package-$(WELL).tar.gz

# ---------------------------------------------------------------- SIH demo (docs/demo.md)
# One command on a laptop with Docker: the stack (dev overlay), a small local LLM, synthetic
# data, accepted facts and search index, detectors (trained once), then the held-out well
# replayed at 300x and the web app opened on its Live Console. Idempotent: rerun any time.
# Only synthetic data; downloads: container images and the LLM (about 1 GB), no well data.
DEMO_WELL ?= SA-06
DEMO_FOCUS ?= loss
DEMO_TOOLS = $(COMPOSE) --profile tools run --rm apitools
WEB_URL = https://localhost:5173

demo: .env
	-$(COMPOSE) --profile tools build
	$(COMPOSE) up -d --wait --wait-timeout 1800
	@$(MAKE) --no-print-directory ca
	. ./.env && $(COMPOSE) exec -T ollama ollama pull $$LLM_MODEL \
	  && $(COMPOSE) exec -T ollama ollama run $$LLM_MODEL "Reply OK" >/dev/null
	$(COMPOSE) exec -T api python -m api.seed
	$(COMPOSE) --profile tools run --rm datatools python seed.py --offline
	$(DEMO_TOOLS) sh -c "cd data && python -m synthetic_assam.load_truth && cd .. && python -m api.assistant index"
	-@$(MAKE) --no-print-directory public-wells
	@test -f data/models/realtime/detectors.joblib \
	  || $(DEMO_TOOLS) python -m api.realtime train --wells 60 --hours 12 --test-wells 12
	@$(MAKE) --no-print-directory demo-replay
	@$(MAKE) --no-print-directory demo-open

# (Re)start the held-out well's replay through the live path, with LLM explanations: the
# 20 minutes before its first recorded DEMO_FOCUS event to 10 minutes after. The advisory
# arrives about 30-60 s after the start.
demo-replay:
	-@docker rm -f wells-demo-replay >/dev/null 2>&1
	$(COMPOSE) --profile tools run -d --name wells-demo-replay apitools \
	  python -m api.replay well --well $(DEMO_WELL) --speed $(SPEED) --explain --focus $(DEMO_FOCUS)
	@echo "replaying $(DEMO_WELL) at $(SPEED)x: docker logs -f wells-demo-replay"

# Open the Live Console of the demo well (prints the address if no browser opener is found).
demo-open:
	@id=$$($(COMPOSE) exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -tAc "SELECT b.id FROM wellbore b JOIN well w ON w.id = b.well_id WHERE w.name = '"'"'$(DEMO_WELL)'"'"' ORDER BY b.id LIMIT 1"' | tr -d '[:space:]'); \
	  url="$(WEB_URL)/#/live/$$id"; echo "Live Console: $$url  (trust .certs/ca.crt; users: docs/demo.md)"; \
	  (xdg-open "$$url" || open "$$url" || cmd.exe /c start "" "$$url") >/dev/null 2>&1 || true

# Step 6 (G3): stop the LLM, replay again at 600x: the alert still fires, on time, from rules.
demo-llm-down:
	$(COMPOSE) stop ollama
	@$(MAKE) --no-print-directory demo-replay SPEED=600

demo-llm-up:
	$(COMPOSE) up -d --wait ollama

demo-down:
	-@docker rm -f wells-demo-replay >/dev/null 2>&1
	$(COMPOSE) down

urls:
	@echo "Trust .certs/ca.crt once (make ca; docs/guide.md), then restart the browser."
	@echo "Web       https://localhost:5173   (planner / planner-dev-password, or Sign up)"
	@echo "API       https://localhost:5173/api/docs"
	@echo "Grafana   https://localhost:3000   (Service levels dashboard)"
	@echo "Keycloak  https://localhost:8444/auth/admin"
	@echo "Dagster   http://localhost:3001"
	@echo "S3        https://localhost:9000   (S3_ROOT_USER / S3_ROOT_PASSWORD in .env)"
	@echo "Docs      make docs -> http://localhost:8001"
