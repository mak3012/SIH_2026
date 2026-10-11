# Runbook: LLM down

**Symptoms:** Prometheus `LLMDown` (most calls fail), `LLMSlow` (p95 > 60 s) or
`ExplanationQueueStuck`; alerts show "explanation failed" or "timeout"; WellMind answers are
marked as rule-composed; briefings come back with status `fallback`; extraction sends more
values to the review queue.

**What still works (by design, [ADR-0002](../../adr/0002-rules-decide-llm-explains.md)):** every
alert is raised, escalated and cleared by rules, on time; the LLM only adds the explanation.
The assistant answers from rule-composed templates, verified the same way. Briefings fall back
to rule-generated text. Nothing that decides waits for the LLM. Tell users that explanations
are missing, not alerts.

## 1. Which backend, and is it up?

```bash
grep -E '^LLM_(BACKEND|URL|MODEL)=' .env
```

| Where | Backend | Check |
|---|---|---|
| Laptop | Ollama (`http://ollama:11434`) | `docker compose exec ollama ollama list` |
| Rig box | llama.cpp behind the proxy (`https://proxy:8081`) | `docker compose logs --since 15m llm` |
| Headquarters | vLLM or another OpenAI-compatible server | its own monitoring |

From the API container (same path the app uses):

```bash
docker compose exec api python -c "import os, urllib.request as u; print(u.urlopen(os.environ['LLM_URL'].rstrip('/') + ('/api/tags' if os.environ['LLM_BACKEND'] == 'ollama' else '/v1/models'), timeout=10).read()[:300])"
```

## 2. Common causes

- **Model not loaded** (Ollama: `model not found`; llama.cpp exits at start): the model file is
  missing. Laptop: `make models`. Rig box: the GGUF named by `EDGE_LLM_MODEL_FILE` must be in
  the `models` volume under `llm/` ([Rig Edge Pack](../edge.md#the-local-model)).
- **Out of memory** (container restarts, `docker inspect llm --format '{{.State.OOMKilled}}'`):
  use a smaller quantisation or model; on a rig box lower the context (`-c`) in the compose
  command.
- **Slow, not down:** one long prompt blocks a small server. `wells_llm_seconds` by `prompt`
  shows which (extraction prompts are the heaviest). Pause Dagster runs at headquarters
  (Dagster UI → Deployment → Sensors: stop `new_documents`) until the queue drains.
- **Certificate errors** (`CERTIFICATE_VERIFY_FAILED` in api logs): an external endpoint's CA is
  not in the containers' trust bundle; add it as in the
  [stale-feed runbook](stale-feed.md#3-certificate-not-trusted).

## 3. Switch to rules only (if the fix will take a while)

```bash
sed -i 's/^LLM_BACKEND=.*/LLM_BACKEND=none/' .env
```

```bash
docker compose up -d api realtime dagster
```

With `none`, extraction auto-accepts nothing (every value goes to review), explanations are
recorded as `disabled`, and the assistant uses rules. Reverse it the same way when the server
is back.

## 4. Catch up

Alerts raised while the LLM was down keep `explanation_status = failed` or `timeout` and are not
explained later: the explanation belonged to that moment, and the alert itself was complete
without it. Alerts raised after recovery are explained as usual. Extraction values that went to
review stay in the review queue for data stewards.

## 5. Close

`LLMDown` clears; `wells_queue_depth{queue="explanation"}` back to 0; a test question in
WellMind shows the LLM as drafter.
