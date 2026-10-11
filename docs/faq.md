# FAQ

Answers to the questions judges and drilling engineers ask most. Each answer gives the short
version first, then where to check it.

## What if the LLM is wrong?

**It cannot change a decision, and a wrong number does not reach the screen.**

- **Rules decide, the model explains** ([ADR-0002](adr/0002-rules-decide-llm-explains.md)).
  Every alert is raised, escalated and cleared by deterministic, versioned rules over measured
  data. The model has no write path to alerts, facts or plans; the API enforces it, and the
  database refuses an alert whose numbers disagree with each other.
- **Every number is verified before it is shown.** Each number, depth and date in an answer,
  an alert explanation or a briefing must match the evidence it cites (at the precision
  written); citations must come from the evidence set. A draft that fails is regenerated once
  with the problems listed; if it fails again the answer falls back to a rule-written one, or
  says "not in the records".
- **Measured:** when one number was changed in 30 % of drafts, the verifier caught **24 of 24**
  changed drafts and rejected **0 of 86** correct ones; answer accuracy stayed **100 %** on the
  100-question set ([Assistant → Evaluation](assistant.md#evaluation)).
- **Documents cannot steer it.** Document text is data, never instructions; the model has no
  tools, no network route out and no credentials
  ([Security → Prompt injection](security.md#prompt-injection-via-documents)).
- **If the model is down or slow**, alerts fire on time (demo step 6), answers are written by
  rules, briefings fall back to rule text, and extraction sends values to the review queue.

What it can still do wrong: word an answer in a misleading way without changing its numbers.
That is why every answer is labelled AI-drafted and its sources are one click away.

## How are depths aligned between wells?

**Every depth goes through one canonical layer before it is stored or compared**
([Canonical depth](canonical-depth.md)).

1. **Units.** Feet, inches, ppg, psi/ft are converted to metres, sg and sg-equivalent
   gradients, and each conversion is logged on the fact's source (`printed "3,665 ft" → 1,117.1 m`,
   factor 0.3048). The evidence viewer shows it.
2. **Datums.** Depths are reported below KB, RT, DF or ground level. Each wellbore stores its
   datum and elevation; TVDSS (true vertical depth below sea level) = TVD − datum elevation, so
   wells drilled from different rigs compare on one reference.
3. **Trajectories.** Measured depth becomes TVD and position by the minimum-curvature method,
   interpolated on the arc between survey stations.
4. **Coordinates.** Kalianpur 1975 and other datums are transformed to WGS 84 with pinned
   transformations; each well carries a datum confidence and an uncertainty radius (the gold
   halos on the map).
5. **Formations.** Names are matched to one ontology ("BARAIL SST", "Brl" → Barail); a model may
   suggest a match, only a rule or a person confirms it.
6. **Formation-relative position.** Offset events are placed at their relative position inside
   the formation (0 = top, 1 = base) and projected onto the active well's prognosed tops: a loss
   10 % of the way into the Barail in an offset lands 10 % of the way into the Barail of the
   plan, even when the formation sits deeper or is thicker there.
7. **Live re-anchoring.** When the geologist picks a top while drilling, every projected hazard
   band moves with it, and the pick is audited.

Worked examples with published reference values are in [Canonical depth](canonical-depth.md).

## How does it integrate with eRTMAC?

**It reads the same real-time feeds eRTMAC already receives, one way, and never writes to rig
systems** ([Real-time → Connectors](realtime.md#connectors)).

| Source | Connector |
|---|---|
| WITSML 1.4.1.1 store (rig or RTOC aggregator) | `WitsmlPoller`: `WMLS_GetFromStore` on time logs, standard mnemonics and units |
| WITS level 0 serial / TCP feed | `Wits0Listener` |
| OPC-UA server on the rig network | `OpcUaClient` (read nodes) |
| An existing historian database | `SqlPoller` (read-only query) |
| ETP 1.2 | interface ready; needs an ETP server to finish against |

- **Placement.** At the real-time operations centre, the real-time service polls the WITSML
  store eRTMAC already aggregates, with a read-only account. On a rig without a reliable link,
  the **Rig Edge Pack** runs the same service on one box next to the rig's WITSML server and
  syncs with headquarters when VSAT is up ([Rig Edge Pack](operations/edge.md)).
- **Outputs.** Alerts and the live console in the web app; the REST API and WebSocket for any
  eRTMAC dashboard that wants them. Nothing is pushed into control systems.
- **Fit.** Idempotent writes (a log replayed twice gives the same rows), stale-feed detection
  within 10 s, and gaps recorded: the feed's quirks do not become false alerts.

## Is the data secure?

**Yes, by design and by default; details in [Security](security.md).**

- **Who sees what:** Keycloak sign-in; roles that separate duties (planner, wellsite engineer,
  superintendent, data steward, admin); every database row filtered by licence block and joint
  venture (row-level security), so a partner never sees another block's wells — not even that
  they exist.
- **On the wire:** TLS on every hop that carries data or credentials, including inside the
  stack; the database refuses unencrypted connections.
- **No egress:** services run on internal networks with no route out; models are loaded locally;
  nothing is sent to a cloud LLM.
- **Audit:** every read of a source document, every edit, decision, acknowledgement and model
  version is in a hash-chained log, anchored outside the database.
- **Threat model:** data exfiltration, prompt injection through documents and insider edits, each
  with its mitigations and the risk that remains.

## What does it cost?

**No licence fees: every component is open source with a permissive or server-side licence
([Licences](licences.md)). The costs are hardware and people.**

Indicative sizing (to be confirmed by a load test on the operator's data):

| Item | Sizing |
|---|---|
| Headquarters server | One server: 16–32 cores, 64–128 GB RAM, 2 TB SSD. Runs the database, ingestion, API and observability for a basin's records |
| LLM | Optional. One 24–48 GB GPU serves a 7B–14B model for the whole organisation; without it, everything runs in rules mode |
| Rig Edge Pack | One rugged small server per rig: 8 cores, 32 GB RAM, 1 TB SSD, no GPU (a 1.5B model on CPU) |
| Bandwidth | A few MB a day per rig (alerts, acknowledgements, voice notes); the hazard package once per mobilisation |
| People | Data stewards to work the review queue during the backfill of old reports (the queue is sorted by value, so the most important fields are checked first); one platform engineer |

The largest cost is the one-time backfill of scanned reports. Typed and scanned pages extract
well (scanned: precision 0.97, recall 0.85 on the synthetic gold set); faxed and handwritten
pages mostly go to people ([Evaluation](evaluation.md#extraction)).

## What is the rollout plan?

Each phase has an exit gate measured on the operator's own wells.

| Phase | Scope | Exit gate |
|---|---|---|
| 0. Data pilot (6–8 weeks) | One asset's WCRs and DDRs ingested; planners use Map, Correlation, Roadmap and Ask, read-only | Extraction precision ≥ 0.95 on a 200-page sample checked by stewards; planners confirm the offset rankings on 3 past wells |
| 1. Planning (1 quarter) | Program Linter and briefings on every new well of that asset | Linter findings reviewed in pre-spud meetings; no blocking finding judged wrong |
| 2. Shadow real-time (1 quarter) | Read-only WITSML feed from 2–3 rigs into the RTOC; alerts go to the RTOC only, not the rig | Replay and shadow results: warning-level recall and false alerts per 24 h at least as good as on synthetic data; RTOC agrees with each alert's evidence |
| 3. Rig go-live | Live Console on the rig (Rig Edge Pack where the link is poor); acknowledgements and shelving by the wellsite engineer | Alert rate within the ISA-18.2 budget; acknowledgement times recorded |
| 4. Scale | More assets and rigs; fleet board for superintendents; models retrained on the operator's own events | Per-asset gates repeated |

Throughout: the operator's data stays on its own servers, and every phase can be rolled back
without touching rig systems.

## Is it all real?

**The software is real; the data is synthetic.** Thirty Upper Assam wells, their reports
(typed, scanned, faxed, handwritten), voice notes and rig data are generated by
`data/synthetic_assam` from geology and drilling statistics, because real Indian well records
are confidential. Every synthetic document says so on the page and in the UI. The evaluation
numbers are measured on that data — read them as an upper bound until replayed on real wells
(Volve and Utah FORGE public data are supported for that).

## How early does it warn?

**Honestly: as a problem starts, not before.** Over 30 held-out wells the first alert came a
median **0.7 minutes after** the recorded start of the event, at warning level or above for
**89 %** of events, with **0.09** false alerts per 24 operating hours, and left a median
**19 minutes** between the first alert and the end of the event. Earlier warning comes from the
plan: the hazard roadmap and the lookahead show where offsets had trouble, with an ETA from the
current rate of penetration ([Evaluation](evaluation.md)).
