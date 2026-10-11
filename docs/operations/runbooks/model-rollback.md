# Runbook: model rollback

**When:** after a new detector bank (`make rt-models`) or a new LLM / prompt, alerts got worse
(more false alerts, missed events in replay, engineers' feedback turning to false positive) or
answers did (Prometheus `VerifierRejectsHigh`, more "not in the records"). Roll back first,
investigate after.

Every model that decides or explains is recorded: the real-time service writes a `model.load`
audit entry with the ruleset, detector version and SHA-256, LLM backend, model and prompt
versions each time it starts; every alert stores `detector_version`; every LLM call is logged
in `llm_call` with model and prompt hash.

## 1. What changed, and when?

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT ts, entity_id AS feed, payload->'detectors' AS detectors, payload->'llm' AS llm FROM audit_log WHERE action = 'model.load' ORDER BY id DESC LIMIT 10"
```

Feedback by detector version (are false positives up since the change?):

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT detector_version, feedback, count(*) FROM alert WHERE raised_at > now() - interval '14 days' GROUP BY 1, 2 ORDER BY 1, 2"
```

## 2. Detector bank

Banks are files with a checksum beside them; the service refuses a file whose SHA-256 does not
match. Keep every released bank (`make rt-models` overwrites the default path, so copy it first):

```bash
mkdir -p data/models/realtime/archive && cp data/models/realtime/detectors.joblib data/models/realtime/archive/detectors-$(date +%Y%m%d).joblib && cp data/models/realtime/detectors.sha256 data/models/realtime/archive/detectors-$(date +%Y%m%d).sha256
```

Roll back by pointing `RT_DETECTORS` at the previous bank and restarting the service:

```bash
sed -i 's#^RT_DETECTORS=.*#RT_DETECTORS=data/models/realtime/archive/detectors-20261001.joblib#' .env
```

```bash
docker compose up -d realtime api
```

Check the new `model.load` entry shows the old version and SHA-256, and the admin page
(`#/admin`, Detector bank) shows it too. Open alerts keep the version that raised them.

No bank at all: the service runs on physics rules only (uncalibrated) and says so in each
alert's evidence. That is a safe fallback if no previous bank is trusted.

Rig boxes take the bank from the hazard package (`models/detectors.joblib`); after rolling back
at headquarters, build a new package (`make edge-package`) and load it on the rig, or set
`RT_DETECTORS` on the rig box the same way.

## 3. LLM or prompt

The model is configuration (`LLM_MODEL`); prompts are versioned files pinned in code
(`apps/api/src/api/prompts/<name>/<version>.md`, `PromptSet` pins), so old versions stay
available.

- **Model:** set the previous `LLM_MODEL` in `.env` and restart `api`, `realtime`, `dagster`.
  Ollama/llama.cpp must still have that model.
- **Prompt:** revert the pin in the code (for example `{"briefing": "v1"}`) and redeploy.
- **Quickest safe state:** `LLM_BACKEND=none` (see [LLM down](llm-down.md#3-switch-to-rules-only-if-the-fix-will-take-a-while)).
  Nothing that decides depends on the LLM.

## 4. Confirm

- Replay the held-out wells against the rolled-back bank and compare with the last report
  (`make evaluation`, then the Replay studio).
- `wells_verifier_rejects_total` back to its usual rate; feedback on new alerts.

## 5. Close

Record which version was rolled back, why, the evidence, and what must be true before it is
tried again (for example a replay result that beats the current bank on warning recall and
false alerts per day).
