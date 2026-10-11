# API

FastAPI service in `apps/api`, served over TLS (uvicorn, behind the `proxy`). Interactive OpenAPI
docs: <https://localhost:5173/api/docs> (the API is served
under `/api` of the web address).

## Authentication

Every endpoint except `/health` and `/metrics` needs a Keycloak access token (realm `wells`):

```
Authorization: Bearer <access token>
```

The token must be signed by the realm (RS256, keys from `OIDC_JWKS_URL`), issued by
`OIDC_ISSUER_PUBLIC` (the URL browsers log in at) and have audience `api` (the `web` client's
audience mapper adds it). From the token the API takes:

| Token content | Becomes |
|---|---|
| `preferred_username` | the user recorded on selections, programs, briefings |
| realm roles `wellsite_engineer`, `superintendent`, `planner`, `data_steward`, `admin`, `edge_sync` | permissions `operate`, `plan`, `review`, `admin`, `sync` ([Security → Identity and roles](security.md#identity-and-roles)); reads need any of these roles or `viewer` |
| realm roles `block:<id>`, `jv:<id>` | the licence blocks and JVs row-level security allows |

Each request runs in one database transaction as `app_rw` with those blocks and JVs
(`api.db.scoped`), so a user never sees, scores or cites a well outside their licences.

Browsers cannot set headers on a WebSocket: `/active/{id}/stream` also accepts the token as
its first message, `{"type": "auth", "token": "..."}` (close code 4401 on failure, 4404 when
the wellbore is not visible).

Every write, and every read of a source document, page image, recording or briefing PDF, is
recorded in the hash-chained audit log with the user ([Security → Audit](security.md#audit)).

For local tests only, `DEV_AUTH=1` lets the headers `X-User`, `X-Roles`, `X-Blocks`, `X-JVs`
stand in for a token. It is off unless set, never set in compose, and logged on every use.

| Status | When |
|---|---|
| 401 | no token, bad signature, wrong issuer or audience, expired |
| 403 | the role does not allow the action, or the token has no application role |
| 404 | not found **or not visible** to the caller's blocks (the API does not say which) |
| 409 | `hazard-profile` / `lookahead` / `lint` before any `/offsets/select` for the wellbore; acknowledging an alert that is not open; an edge batch whose audit chain does not continue |
| 422 | invalid request (unknown weight name, pinned and excluded, bad program) |

## Endpoints

| Method and path | Permission | Does |
|---|---|---|
| `GET /health` | – | Liveness, used by the container healthcheck |
| `GET /metrics` | – | Prometheus metrics |
| `GET /wells?near=…&radius_m=…` | any | Wells within a radius of a point or of a well |
| `GET /wellbores` | any | Every wellbore the caller may see: block, status, TD, offsets selected, feed state, open alerts |
| `GET /wellbores/{id}/formations` | any | Formations along the wellbore (targets for offset selection) |
| `POST /offsets/select` | plan | Rank analogue wells, apply pins / exclusions / weights, store the choice |
| `GET /active/{wellbore_id}/hazard-profile` | any | Hazard roadmap for the latest (or a given) selection |
| `GET /active/{wellbore_id}/lookahead` | any | Distance and ETA to each hazard band |
| `POST /programs/lint` | plan | Rule findings for a drilling program |
| `POST /briefings/{wellbore_id}` | plan | Verified briefing (or rule fallback) with a PDF |
| `GET /briefings/{wellbore_id}/{id}.pdf` | any | The briefing PDF (read audited) |
| `WS /active/{wellbore_id}/stream` | any | Live channels, rig state, alerts, feed state |
| `GET /active/{wellbore_id}/alerts` | any | Open and recent alerts (`include_suppressed` for shelved / over budget) |
| `GET /active/{wellbore_id}/feed` | any | Connector state and data gaps |
| `POST /alerts/{id}/ack` | operate | Acknowledge an open alert |
| `POST /alerts/{id}/feedback` | operate | True / false positive / unsure, with a note |
| `POST /active/{wellbore_id}/shelves` | operate | Shelve a hazard with a reason, at most 720 min |
| `DELETE /shelves/{id}` | operate | Unshelve |
| `GET /wellbores/{id}/scene` | any | 3D subsurface scene: trajectories, tops, events, hazard bands, bit (local metres) |
| `GET /offsets/preview/{wellbore_id}?formation=&surface_radius_m=&subsurface_radius_m=` | any | Offset map: every visible well (surface, datum confidence and position uncertainty, fault block, evidence count, trajectory in lon/lat/z, where it crosses the target top) and the analogue ranking with per-factor scores. Read-only; selecting stays `POST /offsets/select` |
| `GET /correlation/{wellbore_id}?datum=` | any | Depth tracks of the well and its offsets (latest selection, else the nearest six): tops, events with their source page, casing shoes, mud weights, ECDs, LOT/FIT, in MD and TVDSS; the well's prognosed tops, projected hazard bands and bit. `datum` defaults to the selection's target formation |
| `GET /active/{wellbore_id}/history?minutes=&max_points=` | any | Recent real-time samples, same shape as the stream, thinned to `max_points` |
| `GET /active/{wellbore_id}/envelope` | any | Torque-and-drag hookload envelope against bit depth (friction factor and ± 0.1) and the MW/ECD window per formation from offset kicks, losses and LOT/FIT, with the records behind each bound |
| `GET /source-refs/{id}` | any | Where a cited value came from: document page and box, or voice-note time span |
| `GET /documents/{id}/file` | any | The source PDF (evidence viewer; read audited) |
| `GET /documents/{id}/fields` | any | Every fact stored from a document, with page, box, confidence and review status, and its open review items |
| `GET /documents/{id}/pages/{n}.png` | any | One page as an image (read audited) |
| `GET /audio-notes/{id}` | any | A voice note and the cited spans with their transcript text |
| `GET /audio-notes/{id}/file` | any | The recording (read audited) |
| `GET /review/items?status=&order=value\|confidence&limit=` | any | Extraction review queue; `value_score` = field importance × (1 − confidence) × (1 + 0.5 per failed rule); `conflict` when a rule failed or the passes disagreed |
| `POST /review/items/{id}` | review | Accept / correct / reject, audited |
| `GET /fleet?digest_hours=` | any | Per rig: feed freshness, depth, rate, next hazard band and ETA, open alerts by tier; shift digest |
| `POST /voice-notes?wellbore_id=&client_id=&duration_s=&formation=&note=&recorded_at=` | operate | Upload a recording captured in the app (raw audio body). Idempotent on `client_id`; queued for transcription and lesson drafting, pending expert confirmation |
| `GET /voice-notes` | any | The caller's captured notes: transcribing, pending confirmation, confirmed or reviewed |
| `GET /admin/status` | admin | Record counts, detector bank, LLM configuration, feeds, audit chain status (entries, head, first broken id) |
| `POST /assistant/ask` | any | Answer a question from the records, with citations, evidence and follow-ups (question and cited records audited) |
| `POST /assistant/ask/stream` | any | The same answer as server-sent events (`status`, `delta`…, `done` or `error`), streamed after verification |
| `POST /assistant/ask/voice` | any | Same for a spoken question (raw audio body); returns the transcript |
| `GET /assistant/conversations[/{id}]` | any | The caller's conversations and their turns |
| `DELETE /assistant/conversations/{id}` | any | Forget a conversation |
| `POST /edge/sync` | sync | A rig box's batch: alerts, shelves, voice notes with recordings, feed state, the box's audit entries. Idempotent; 409 when the box's audit chain does not continue ([Rig Edge Pack](operations/edge.md)) |
| `GET /edge/package/{wellbore_id}?files=` | sync | Hazard package (`tar.gz`) within the account's licences |

Formulas, parameters and the meaning of every number: [Intelligence](intelligence.md). The
WebSocket protocol, alarm policy and alert numbers: [Real-time](realtime.md#api). Assistant
pipeline, response format, verifier and refusal rules: [Assistant](assistant.md#api).

### `GET /wells`

`near` is `lat,lon` (WGS 84) or `well:<name>`; `radius_m` default 5000, max 100 000. Example
response (demo wells):

```json
[{"well_id": 1, "well": "DEMO-1", "block": "AA-ONN-2001/1", "lat": 27.1, "lon": 94.9,
  "datum_confidence": null, "position_uncertainty_m": null, "distance_m": 0.0},
 {"well_id": 3, "well": "DEMO-3", "block": "AA-ONN-2001/1", "lat": 27.085, "lon": 94.915,
  "datum_confidence": null, "position_uncertainty_m": null, "distance_m": 2230.4}]
```

### `POST /offsets/select`

```json
{"active_wellbore_id": 42, "formation": "Barail", "surface_radius_m": 10000,
 "subsurface_radius_m": 5000, "weights": {"vintage": 0}, "pinned": [7], "excluded": [5],
 "max_offsets": 10}
```

`formation` may be an id, name, synonym or abbreviation. Weights not given keep their defaults;
unknown names are refused. Response: `selection_id`, the weights and scales used, and every
candidate with `rank`, `score`, `included`, `pinned`, `excluded`, `within_radii`, `surface_m`,
`formation_m` and `factors` (name, weight, raw, score, unknown, contribution, why).

### `GET /active/{wellbore_id}/hazard-profile`

Optional `selection_id` (default: the latest for the wellbore). Response: `offsets` (well,
weight), `formations` along the plan (top and base MD / TVDSS), `cells` (one per formation ×
hazard: `mean`, `ci90`, `n_eff`, `prior`, `counts` {event, none, unknown, not_reached},
`evidence_state`, `severity`, `rel_position`, `band_md`, `band_tvdss`, `mitigations`,
`evidence`), and the `parameters` used.

### `GET /active/{wellbore_id}/lookahead`

Optional `selection_id`, `min_mean` (0.1), `window_h` (12). Response: `current_md`,
`rate_m_per_hr`, `rate_basis`, `as_of`, and `bands` (formation, hazard, mean, ci90, band_md,
state `ahead` / `in band` / `passed`, `distance_m`, `eta_hours`, `eta`).

### `POST /programs/lint`

Body: `{"program": {...}, "selection_id": null}` (program schema in
[Intelligence §4](intelligence.md#4-program-linter)). Response: `lint_run_id`, `program_id`,
`ruleset`, `findings` (rule, severity, message, section, formation, numbers, evidence ids),
`evidence` (id, kind, well, formation, md, value, unit, source {document, page,
source_ref_id}), and a `summary` count per severity.

### `POST /briefings/{wellbore_id}`

Body: `{"lint_run_id": 17}`. Response: `briefing_id`, `status` (`verified` / `fallback`),
`briefing` (title, paragraphs with citations), `verification` (problems found per attempt; the
last list is empty), `model`, `pdf_url`.

## Database

- Schema: Alembic migrations in `apps/api/migrations/versions` (plain SQL). The container runs
  `alembic upgrade head` on every start. New migration:
  `uv run alembic -c apps/api/alembic.ini revision -m "what changes"`.
- Every request that reads or writes user data opens its transaction with
  `api.db.scoped(conn, role, blocks, jvs)`: it switches to `app_rw` (or `app_ro` for chat
  text-to-SQL) and sets the ABAC attributes that row-level security checks. It refuses to nest,
  because `SET LOCAL` outlives savepoints.

```python
with scoped(conn, "app_rw", blocks=user.blocks, jvs=user.jvs):
    conn.execute("SELECT name FROM well")  # only the caller's wells
```

- One connection per request for now; add a pool (psycopg_pool) when request rates need it.

See [Data model](data-model.md).
