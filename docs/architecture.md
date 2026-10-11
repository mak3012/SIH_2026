# Architecture

Four zones. Data moves one way across the OT boundary: out of the rig, never back in.

```mermaid
flowchart LR
    subgraph OT["OT boundary (rig site)"]
        RIG["Rig sensors / mud logging"]
        WITSML["WITSML server"]
        ERTMAC["eRTMAC real-time feed"]
        RIG --> WITSML --> ERTMAC
    end

    subgraph ING["Ingestion"]
        UPLOAD["Scan upload<br/>(WCR, DDR PDFs)"]
        STREAM["Real-time receiver<br/>(read-only, one-way)"]
        DAG["Dagster pipelines<br/>OCR · extraction · validation"]
    end

    subgraph DATA["Data"]
        S3[("S3 object store<br/>raw files, scans")]
        subgraph PG["PostgreSQL (ADR-0001) · RLS by block/JV"]
            REL[("Relational<br/>wells, facts, provenance")]
            TS[("TimescaleDB<br/>channels")]
            GEO[("PostGIS<br/>locations, trajectories")]
            VEC[("pgvector<br/>memory embeddings")]
        end
    end

    subgraph INT["Intelligence + Serving"]
        RULES["Rule engine<br/>(decides)"]
        LLM["LLM<br/>(explains; ADR-0002)"]
        API["FastAPI"]
        WEB["React UI"]
        KC["Keycloak"]
    end

    USER(["Engineer"])

    %% Flow 1: scan -> fact -> memory
    UPLOAD -- "1 scan" --> S3
    S3 -- "1" --> DAG
    DAG -- "1 fact + provenance" --> REL
    REL -- "1 embed" --> VEC

    %% Flow 2: eRTMAC -> alert
    ERTMAC -- "2 one-way" --> STREAM
    STREAM -- "2" --> TS
    TS -- "2" --> RULES
    RULES -- "2 alert" --> REL
    RULES -. "2 explain" .-> LLM
    REL -- "2 alert" --> API

    %% Flow 3: question -> cited answer
    USER -- "3 question" --> WEB --> API
    API -- "3 retrieve" --> VEC
    API -- "3 retrieve" --> REL
    API -- "3 context" --> LLM
    LLM -- "3 cited answer" --> API --> WEB

    KC -. "OIDC" .-> WEB
    KC -. "JWT" .-> API
```

## Flows

**1. Scan → fact → memory.** A scanned report lands in the S3 object store. A Dagster pipeline extracts
values (depths, casing points, mud weights, NPT events), validates them against depth maths and
units in `packages/core`, and writes each value as a fact that points back to the page and region
it came from. Facts are embedded into pgvector so they can be recalled later.

**2. eRTMAC → alert.** Real-time channels cross the OT boundary one way (WITSML, WITS0,
OPC-UA or SQL connectors, all read-only) into TimescaleDB. Rig state, derived channels (MSE,
ECD, torque & drag) and calibrated detectors give a likelihood ratio; the hazard roadmap gives
the prior; rules turn the posterior into advisory / warning / critical alerts under an
ISA-18.2-style policy. The alert is stored and pushed to the live view first; only then may the
LLM add an explanation, and it cannot create, suppress or change an alert (ADR-0002).

**3. Question → cited answer.** A question (typed or spoken) is answered from rows of the
read-only `chat.v_*` views (rule-planned or model-written SQL, run as `app_ro` under the
caller's licences) and from passages found by hybrid retrieval (full-text + pgvector). The LLM
drafts only from that evidence; a rule-based verifier checks every number, depth and date
against the evidence each sentence cites, else the answer is regenerated or becomes "not in the
records". Citations point to the document page and bbox, or the voice-note time span. Details:
[Assistant](assistant.md).

Tables, provenance rules and access control: [Data model](data-model.md). Flow 1 in detail
(assets, rules, confidence, voice notes): [Pipelines](pipelines.md). Analogues, hazard roadmap,
lookahead, program linter and verified briefings: [Intelligence](intelligence.md). Flow 2 in
detail (connectors, simulator, detectors, alarm policy, WebSocket): [Real-time](realtime.md).

## Zones

| Zone | Runs | Trust |
|---|---|---|
| OT boundary | Rig systems, WITSML, eRTMAC | Not ours. Read-only, outbound only. |
| Ingestion | Upload endpoint, stream receiver, Dagster | Untrusted input is parsed and validated here. |
| Data | PostgreSQL, S3 object store (SeaweedFS) | Source of truth. |
| Intelligence + Serving | Rules, LLM, API, UI, Keycloak | Every request authenticated; each role may do only its own actions ([ADR-0003](adr/0003-roles-separate-duties.md)). |
| Ingress | Caddy proxy | The only published ports; TLS in and TLS to every upstream. |
| Rig box | The same stack on one machine, offline-first | Syncs with headquarters through the API when the link is up ([ADR-0004](adr/0004-rig-sync-through-the-api.md)). |

Every service sits on internal networks with no route out; TLS on every hop that carries data
or credentials; every sensitive read, edit, acknowledgement and model load in the hash-chained
audit log; traces (OpenTelemetry → Tempo) across ingestion, the alert path and chat, and
service-level metrics in Prometheus and Grafana. Details and the threat model:
[Security](security.md); procedures: [Operations](operations/index.md).
