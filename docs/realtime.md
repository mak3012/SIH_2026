# Real-time

Live drilling data in, alerts out. The rule from [ADR-0002](adr/0002-rules-decide-llm-explains.md)
holds here too: **rules and calibrated statistics decide** whether there is an alert and how
serious it is; **the LLM only explains** it, afterwards, and the alert never waits for it.

Code: `apps/api/src/api/realtime/`

| Module | Does |
|---|---|
| `connectors.py` | WITSML 1.4.1.1 store polling, ETP 1.2 (stub), WITS0 TCP listener, OPC-UA client, SQL poll |
| `simulator.py` | WITSML store that replays a synthetic, FORGE or Volve well at 1×–600× |
| `synthetic.py` | Synthetic rig data with labelled hazard episodes (training, tests, demo) |
| `store.py` | Idempotent writes keyed (wellbore, ts, channel) |
| `rigstate.py` | Rig-state rules |
| `derived.py` | MSE, d-exponent, Herschel-Bulkley ECD, soft-string torque & drag, friction calibration |
| `features.py` | Windowed features, per-hazard gates and physics rules |
| `detectors.py` | IsolationForest + XGBoost, isotonic calibration into likelihood ratios, group K-fold training, evaluation |
| `fusion.py` | Prior × LR, tiers and alarm policy |
| `pipeline.py` | One sample at a time through all of the above (no I/O; training uses the same code) |
| `context.py` | What the pipeline knows about the wellbore: survey, tops, casing, mud, roadmap priors, friction |
| `service.py` | The running service: connector → store → pipeline → alerts → NOTIFY; heartbeat; explainer queue |
| `explainer.py` | The LLM "why", verified |
| `routes.py` | WebSocket `/active/{id}/stream` and the alert endpoints |

Units inside are canonical (m, kN, kN·m, bar, L/min, m³, sg, m/h); feeds are converted on the
way in. Depths are MD.

## One alert, end to end

```mermaid
sequenceDiagram
    autonumber
    participant Rig as Rig WITSML store
    participant Svc as Real-time service
    participant PG as PostgreSQL
    participant API as API WebSocket
    participant UI as Live view
    participant LLM as LLM explainer

    loop every 2 s
        Svc->>Rig: WMLS_GetFromStore(log, startDateTimeIndex = last ts)
        Rig-->>Svc: new rows
    end
    Svc->>Svc: rig state, derived channels, 1/5/10/30 min features
    Svc->>Svc: physics rule + XGBoost/IsolationForest → calibrated LR
    Svc->>Svc: prior (roadmap, current formation) × LR → posterior → tier, on-delay held
    Svc->>PG: UPSERT rt_measurement (idempotent)
    Svc->>PG: INSERT alert (committed)
    Svc->>PG: NOTIFY rt_wellbore_<id> {alert raise}
    PG-->>API: notification
    API-->>UI: {"type": "alert", "event": "raise", ...}
    Note over UI: The alert is on screen.<br/>Nothing below can delay or change it.
    Svc->>LLM: explain(alert, evidence) — queued, async, 30 s timeout
    alt answer passes verification
        LLM-->>Svc: why + citations [R1]
        Svc->>PG: UPDATE alert SET llm_explanation, explanation_status = 'done'
    else error, timeout, unverifiable
        Svc->>PG: UPDATE alert SET explanation_status = 'failed' / 'timeout'
    end
    Svc->>PG: NOTIFY {alert explained}
    PG-->>API: notification
    API-->>UI: explanation (or "no explanation; the alert stands on the rules")
```

Steps 7–10 happen before the explainer is even queued. `test_alerts_fire_when_the_llm_dies…`
kills the LLM service halfway through a replay and checks that later alerts are still stored
and published, and that every alert's `raise` message precedes its `explained` message.

## Connectors

All connectors are read-only: data crosses the OT boundary one way. Credentials come from the
environment (`WITSML_USER`/`WITSML_PASSWORD`, `OPCUA_USER`/`OPCUA_PASSWORD`, `RT_SQL_DSN`), never
from `rt_feed.config`.

| Connector | Protocol | Notes |
|---|---|---|
| `WitsmlPoller` | WITSML 1.4.1.1 SOAP `WMLS_GetFromStore`, time-indexed `log` | Polls every `interval` s for rows after the last timestamp received (startIndex is inclusive, so the last row is filtered). Mnemonics and units from `logCurveInfo`/`unitList` |
| `Etp12Client` | ETP 1.2 | **Interface stub.** The docstring lists the session and ChannelStreaming steps; raises `NotImplementedError`. No ETP server was available to test against |
| `Wits0Listener` | WITS level 0 over TCP | Frames `&&` … `!!`, lines of 4-digit item + value. Record 01 items below. WITS0 sends no units: the rig's unit set is configuration (`--units`, metric by default) |
| `OpcUaClient` | OPC-UA (asyncua) | Reads the configured node per channel every `interval` s; sample time = the newest source timestamp |
| `SqlPoller` | PostgreSQL historian | A read-only query returning `ts` plus channel columns for rows after `%(since)s` |

WITS0 record 01 items read: 0105 date, 0106 time, 0108 bit depth, 0110 hole depth, 0112 block
position, 0113 ROP, 0114 hookload, 0116 WOB, 0118 torque, 0120 RPM, 0121/0122 SPP, 0127 active
pit volume, 0130 flow out, 0131 flow in, 0133 mud density in.

WITSML mnemonics (any of): bit depth `DBTM`; hole depth `DMEA`, `DEPTH`; block `BPOS`; hookload
`HKLA`, `HKLD`; WOB `WOBA`, `SWOB`; RPM `RPMA`, `SRPM`; torque `TQA`, `STOR`; SPP `SPPA`; flow
in `TFLO`, `MFIA`; flow out `MFOA`; pit volume `TVA`, `TVT`; mud density `MDIA`; ROP `ROPA`.
Unknown mnemonics are ignored; a unit that cannot convert (e.g. torque in amps) drops that
channel with a warning.

### Idempotent writes

One `rt_measurement` row per (wellbore, ts). A write sets only the channels it carries: typed
columns keep their stored value when the new one is NULL, and the jsonb `channels` merges key
by key. Replaying a log, a poller seeing a row twice, or two connectors delivering different
channels at the same timestamp all end in the same row. Derived channels (ECD, MSE, dxc, flow
difference, hookload prediction and deviation, sticking timer) are stored with the sample.

### Heartbeat, gaps, DATA STALE

| Situation | Detected by | Result |
|---|---|---|
| No new sample for `stale_after_s` (6 s) of wall-clock time | service watchdog (0.5 s tick) | `rt_feed.state = 'stale'`, an `rt_gap` row (kind `stale`), `{"type": "feed", "state": "stale"}`; detector windows are cleared so no window spans the silence |
| The service itself is dead | the API WebSocket (no sample for `RT_STALE_AFTER_S`) | the API sends `stale` on its own |
| Network to the browser is down | the live view (no sample for `stale_after_s`) | the page greys itself |
| Sample time jumps by more than 60 s | pipeline | `rt_gap` kind `data_gap`; windows cleared |
| Connector raises | service | `state = 'disconnected'`, reconnect with backoff 1 → 30 s |

Three independent watchdogs, so the screen turns grey **within 10 s** whichever part fails;
stale numbers are never shown as live. Tested end to end (`test_…stale_feed_is_flagged`,
`test_websocket_streams_and_turns_stale`, and the browser test `tests/e2e/live-stale.spec.ts`).

## Simulator

```
python -m api.realtime simulate --source synthetic --wellbore-id 7 --speed 60 --port 8090
python -m api.realtime simulate --source forge --path "data/raw/forge/1283/16A(78)-32_time_data_10s_intervals.csv" --speed 600
python -m api.realtime simulate --source volve --path "data/raw/volve/WITSML Realtime drilling data" --speed 120
```

A WITSML 1.4.1.1 store (`POST /witsml/store`: `WMLS_GetFromStore` for its one log,
`WMLS_GetVersion`) that releases rows as a replay clock reaches them, at 1×–600×. Timestamps
are rebased so the first sample is "now". The real-time service reads it with the same
`WitsmlPoller` it would use on a rig.
The held-out-well replay (`python -m api.replay well`) generates its ticks so that the
recorded history ends when the replay starts: alerts and the shift digest carry today's dates.

| Source | Data | Status |
|---|---|---|
| `synthetic` | A seeded wellbore from the database (survey, tops, casing, events: events happen when the bit reaches their depth), or a random training well | Tested |
| `forge` | Utah FORGE 16A(78)-32 10 s Pason CSV (`download_forge.py --with-timeseries`) | Columns mapped by header keywords and units in brackets; **not yet run on the real file** (not downloaded) |
| `volve` | Volve WITSML real-time log XML files | Same parser as the poller; depth-indexed logs skipped; **not yet run on the real files** |

## Rig state

Rules in order; the first match wins and its id is stored with every evaluation. Per-rig
thresholds (`RigParams`): on bottom when hole − bit < 0.5 m; in slips when hookload < 300 kN;
WOB > 5 kN; rotating when RPM > 10; pumps on when flow in > 100 L/min **and** SPP ≥ 5 bar;
moving when |bit velocity| > 0.03 m/s (velocity from successive bit depths, + = down).

| Rule | State | Condition |
|---|---|---|
| RS-0 | unknown | bit or hole depth missing |
| RS-1 | connection | hookload below the slips threshold (string in slips) |
| RS-2 | drilling_rotary | on bottom, WOB, pumps on, rotating |
| RS-3 | drilling_slide | on bottom, WOB, pumps on, not rotating |
| RS-4 | reaming | off bottom, pumps on, rotating, pipe moving |
| RS-5 | tripping_in | off bottom, pumps off, moving down |
| RS-6 | tripping_out | off bottom, pumps off, moving up |
| RS-7 | tripping_in | off bottom, pumps on, not rotating, moving down (washing down) |
| RS-8 | tripping_out | off bottom, pumps on, not rotating, moving up (pumping out) |
| RS-9 | circulating | pumps on, pipe not moving |
| RS-10 | static | pumps off, pipe not moving |

Calibrate `slips_hookload_kn` on each rig from the hookload seen in slips during the first
connections: it is the block weight plus a margin.

## Derived channels

| Channel | Formula | Notes |
|---|---|---|
| MSE (MPa) | WOB/A + 2π N T / (A · ROP), Teale 1965; N rev/s, ROP m/s | drilling states, ROP > 0.5 m/h; bit size from the program, else bit runs, else 8½ in |
| dxc | d = log(R/60N) / log(12W/10⁶D) (R ft/h, W lbf, D in); dxc = d · 1.03 / ECD | Jorden & Shirley, Rehm & McClendon; ECD in place of mud weight when available |
| ECD (sg) | MW + ΔP_annulus / (ρ_w g TVD) | below |
| Flow out − in (L/min) | flow_out − flow_in | needs a return-flow meter in L/min (a paddle in % is not converted) |
| Pit trend | pit gain over 5 and 10 min (m³), least-squares slope over 5 min (m³/h) | features |
| Hookload prediction and deviation | soft-string model for the current state | below |
| Sticking timer (s) | time the pipe has been neither moving nor rotating with the bit in open hole (below the last shoe) | reset on any movement |

**ECD, Herschel-Bulkley.** τ = τ_y + K γⁿ. Rheology from the latest mud check: with Fann 600/300/6/3
readings τ_y = 2θ₃ − θ₆, n = 3.32 log((θ₆₀₀ − τ_y)/(θ₃₀₀ − τ_y)), K = (θ₃₀₀ − τ_y)/511ⁿ (×0.511
to Pa); with only PV/YP, the Bingham case n = 1, K = PV, τ_y = YP. Annulus sections: casing ID
above the last shoe, bit size below, BHA OD (6½ in, 150 m) over the BHA, 5 in pipe above. Each
section is a narrow slot (gap (D_h − D_p)/2, width π(D_h + D_p)/2); for a pressure gradient G
the slot flow of an HB fluid is integrated in closed form and G is found by bisection so the
flow equals the pump rate. Laminar only (a `ponytail:` note marks where a turbulent branch
goes). Check: the Newtonian limit equals 12 μ Q / (W h³) to 10⁻⁶.

**Torque & drag, soft string** (Johancsik 1984). From the bit up in 30 m elements, buoyed
weight w = w_air (1 − ρ_mud/7.85):

```
N = sqrt((T Δφ sin θ)² + (T Δθ + w ds sin θ)²)
pick-up:  T_top = T_bot + w ds cos θ + μ N        slack-off: … − μ N
rotating: T_top = T_bot + w ds cos θ,  M_top = M_bot + μ N r
```

Hookload = string tension at surface + block weight; drilling subtracts WOB at the bit. The
prediction used depends on the rig state (pick-up when tripping out, slack-off when tripping in,
rotating otherwise, block only in slips); **deviation** = measured − predicted, and the features
divide it by the free string weight.

**Friction factor, offset-calibrated.** For each selected offset well with real-time data,
μ is fitted (golden-section search on 0.05–0.6) to its tripping hookloads with its own survey;
the active well uses the median of those μ weighted by analogue score. No offset data: 0.25. The
source is recorded (`Context.mu_source`). A single μ for cased and open hole — a stated
simplification.

## Detectors

### Gating

A detector runs only in the rig states where its signature means something; outside them the
hazard's LR is 1 (the posterior is the prior) so connections, trips and pump changes do not
raise drilling alarms.

| Hazard | Rig states |
|---|---|
| kick, loss, pack_off | drilling_rotary, drilling_slide, circulating, reaming |
| washout | drilling_rotary, drilling_slide, circulating |
| stuck_pipe | tripping_out, tripping_in, connection, static, reaming, circulating |
| tight_hole | tripping_out, tripping_in, reaming |

### Features

Computed every 5 s of data time once 2 min of history exist; windows 1, 5, 10, 30 min back from
the newest sample. SPP is normalised by (flow/2200)^1.8 so pump-rate changes do not look like
pressure changes.

| Feature | Meaning |
|---|---|
| `dflow_rel_1m`, `dflow_rel_5m` | mean (flow out − flow in) / flow in |
| `dflow_slope_5m` | trend of the above, per minute |
| `pit_gain_5m`, `pit_gain_10m`, `pit_slope_5m` | pit change (m³) and slope (m³/h) |
| `spp_rel_1m`, `spp_rel_30m` | normalised SPP, last minute vs 10 / 30 min median, − 1 |
| `spp_slope_30m` | normalised SPP trend over 30 min, relative per hour |
| `torque_rel_1m`, `torque_cv_1m` | torque vs its 10 min median; coefficient of variation |
| `hl_dev_rel_1m`, `hl_dev_rel_max_1m` | hookload deviation from the T&D prediction / free string weight |
| `bit_speed_1m` | mean \|bit velocity\| |
| `stick_min` | sticking timer, minutes |
| `rop_rel_1m`, `mse_rel_1m` | vs 10 min median |
| `flow_change_1m`, `pumps_age_min` | recent pump-rate change; minutes since the last > 10 % change |
| `state_*` | one-hot rig state |

### Physics rules

Each gives a score in [0, 1] (a feature for the model and the fallback detector) and a fired flag.
"Settled" = pumps unchanged for 2 min (return flow lags a rate change by 1–2 min).

| Hazard | Fires when | Score |
|---|---|---|
| kick | settled, dflow > +5 % and pit gain (5 min) > 0.5 m³ | clip(dflow/0.10) · clip(gain/1 m³) |
| loss | settled, dflow < −10 % and pit slope < −1 m³/h | clip(−dflow/0.3) · clip(−slope/5) |
| pack_off | SPP +10 % and (torque +15 % or torque CV > 0.12) | clip(spp/0.25) · max(clip(torque/0.3), clip(cv/0.2)) |
| washout | settled, SPP trend < −5 %/h and SPP 4 % below its 30 min median | clip(−trend/0.15) · clip(−spp/0.08) |
| stuck_pipe | pipe still and hookload deviation > 25 %, or sticking timer > 20 min | max(overpull term, clip((timer − 5)/25)) |
| tight_hole | overpull > 8 % tripping out / reaming (slack-off < −8 % tripping in) | clip(deviation/0.2) |

### Models and calibration

Per hazard, on the evaluations its gate admits:

1. **IsolationForest** (50 trees) fitted on normal windows → anomaly score.
2. **XGBoost** (200 trees, depth 4) on [features, physics score, anomaly score] → p_raw.
3. **Group K-fold by wellbore** (5 folds): every training well is scored by models fitted
   without it (IsolationForest refitted inside each fold too). Never random rows: neighbouring
   windows of one well are nearly identical, so row-wise folds would leak and overstate skill.
   The code asserts no well is on both sides of a split.
4. **Isotonic regression** on the out-of-fold p_raw → calibrated p (monotone, no shape assumed).
5. **Likelihood ratio** LR = [p/(1 − p)] / [π/(1 − π)], π = the training base rate in gated
   windows. Dividing out π makes the LR independent of how often hazards were in the training
   set (deliberately often, so there are positives); the real-world rate comes from the prior.
   p is clipped to [1/(n+2), 1 − 1/(n+2)] and the LR to [10⁻³, 10⁴].

The bank is saved with a SHA-256 next to it; loading refuses a mismatch (joblib unpickles code,
so only load files this project trained).

**No trained bank** (fresh install): physics rules alone, LR = 10^(4 × score) when the rule
fires, else 1, marked `calibrated: false` on the alert. Conservative and uncalibrated: run
`make rt-models`.

**Physics floor** (tight hole only): when the physics rule fires, the LR is at least
10^(4 × score), whatever the model says. Why and the measured effect: Results, below.

**Sensor resolution.** The generator reports every channel at a sensor's resolution (flow
1 L/min, hookload 0.1 kN, pit 0.01 m³, …) and the flow-difference feature works at 1 L/min. An
earlier model learned that 10⁻¹⁷ L/min "flow" with the pumps off meant stuck pipe and went blind
behind a real feed that rounds such values to 0; `test_lr_survives_feed_rounding` guards this.

Training data are synthetic (`synthetic.py`): drilling stands, connections, wiper trips, with
true friction, lagged return flow, connection flowback, pump-rate changes, pit transfers,
sensor spikes and dropouts as nuisances, and labelled episodes of each hazard (two scheduled
per well plus random ones).

### Results

`make rt-models` writes this section (training on 60 synthetic wells × 12 h, events evaluated
on 12 held-out wells through the full pipeline and alarm policy). The end-to-end measure, with
leave-one-well-out priors and the whole live path (simulator, WITSML poller, service, database),
is the replay evaluation in [Evaluation](evaluation.md#alerts-leave-one-well-out-replay).

<!-- metrics:start -->
Model `detectors@1.0.0`, 484,328 training evaluations, 5-fold group K-fold by well.

| Hazard | Wells | Windows | Positives | AUC (physics only) | AUC (out-of-fold) | PR-AUC | Brier raw → calibrated |
|---|---|---|---|---|---|---|---|
| kick | 60 | 391,958 | 5,951 | 0.935 | 0.990 | 0.942 | 0.0017 → 0.0016 |
| loss | 60 | 391,958 | 13,424 | 0.986 | 0.998 | 0.993 | 0.0010 → 0.0009 |
| pack_off | 60 | 391,958 | 4,151 | 0.987 | 0.995 | 0.978 | 0.0006 → 0.0006 |
| washout | 60 | 384,749 | 49,012 | 0.928 | 0.985 | 0.960 | 0.0153 → 0.0152 |
| stuck_pipe | 60 | 132,653 | 12,266 | 0.961 | 1.000 | 1.000 | 0.0002 → 0.0001 |
| tight_hole | 60 | 20,977 | 1,669 | 0.964 | 0.991 | 0.881 | 0.0199 → 0.0181 |

Event level on held-out wells (130.9 h of data, full pipeline and alarm policy, default prior; physics-only baseline in brackets):

| Hazard | Episodes | Detected (advisory+) | Detected (warning+) | Median delay (min) | False alerts / 12 h |
|---|---|---|---|---|---|
| kick | 3 | 3 [2] | 3 [2] | 2.0 [7.1] | 0.09 [0.0] |
| loss | 7 | 7 [6] | 7 [6] | 0.8 [1.5] | 0.0 [0.0] |
| pack_off | 9 | 9 [5] | 9 [4] | 0.8 [2.9] | 0.0 [0.0] |
| washout | 12 | 12 [4] | 12 [2] | 4.8 [15.0] | 0.09 [0.0] |
| stuck_pipe | 11 | 11 [2] | 11 [2] | 0.2 [0.0] | 0.0 [1.19] |
| tight_hole | 16 | 15 [15] | 9 [9] | 1.0 [1.0] | 0.0 [0.0] |
<!-- metrics:end -->

Reading the results:

- Kick, loss, pack-off, washout and stuck pipe: every held-out episode reached a warning, mostly
  within 1–5 min of onset, with under 0.1 false alerts per 12 h shift. Physics rules alone
  miss many of them because, at realistic priors, an uncalibrated rule rarely carries enough
  evidence, and they give false stuck-pipe alerts (long static periods such as a kick shut-in).
- **Tight hole needs the physics floor.** Its episodes are short (1–3 min of overpull), so the
  1-min windows leave the model little signal: on its own it caught 4 of 16 episodes (none at
  warning) where the physics rule caught 15. For tight hole only, a fired physics rule therefore
  sets a minimum LR of 10^(4 × score) under the model's (`pipeline.PHYSICS_FLOOR`; the alert's
  detector detail says `physics_floor: true`). Same held-out wells: 15 of 16 detected, 9 at
  warning, false alerts unchanged (0). The floor is not used for other hazards: there the model
  is better than the rule (stuck pipe from long static periods, for one, raises false alerts
  under the rule alone).

These numbers measure the method on synthetic data whose physics the features were designed
around; they are an upper bound, not field performance. Field calibration needs labelled rig
data: the `feedback` endpoint records true/false positives for that.

## Fusion

```
posterior odds = prior odds × LR
```

**Prior from the hazard roadmap.** The roadmap ([Intelligence](intelligence.md#2-hazard-roadmap))
gives p_formation, the chance of the hazard somewhere in the formation the bit is in. The
detector's LR is about "this moment is inside an episode", so the prior is converted:

```
p_window = p_formation × episode_h / formation_h      (≤ p_formation)
```

episode_h = 0.25 h (episodes are labelled for their whole duration); formation_h = formation
thickness / rolling ROP, at least 1 h, 10 h when the base is unknown. No roadmap (no offset
selection for the wellbore): p_formation = 0.10, recorded as source `default`. The alert stores
prior, LR and posterior, and the database refuses a row where they do not satisfy Bayes' rule.

Worked example: Barail, loss p_formation 0.40 (roadmap), 300 m thick, ROP 20 m/h → 15 h →
p_window = 0.40 × 0.25 / 15 = 0.0067 (odds 0.0067). Detector LR 180 → posterior odds 1.21 →
**0.55, warning**. The same LR in a formation with p_formation 0.05 gives 0.13: no alert.

## Alarm policy (ISA-18.2 principles)

| Principle | Implementation | Default |
|---|---|---|
| Prioritisation | tiers from the posterior: advisory ≥ 0.20, warning ≥ 0.50, critical ≥ 0.80 | `AlarmPolicy.thresholds` |
| On-delay (chatter) | a tier must hold for 15 s of data time before it is raised or escalated | `on_delay_s` |
| Deadband + off-delay | returns to normal only after the posterior stays below 0.10 (half the advisory threshold) for 120 s | `clear_ratio`, `off_delay_s` |
| No duplicates | one alert per wellbore and hazard while active; escalation updates it (tier, numbers, `updates`) | — |
| Re-arm | a re-raise within 15 min of returning to normal reopens the same alert | `rearm_s` |
| Shelving | per hazard, with a reason (≥ 5 characters), by a named user, for at most 12 h (database check). Shelved advisories and warnings are recorded with `suppressed = 'shelved'` and not delivered. **Critical is never shelved** | `alert_shelf` |
| Alarm-rate budget | at most 10 delivered advisories per wellbore per 12 h shift (06:00 / 18:00 IST); more are recorded as `suppressed = 'budget'`. Warnings and criticals are never budgeted | `advisory_budget` |
| Acknowledgement | `POST /alerts/{id}/ack` (wellsite engineer or superintendent), audited | — |
| Rationalisation data | every alert stores its evidence (features, physics rule, detector output, prior source, offset evidence) and takes engineer feedback | `alert.evidence`, `feedback` |

Suppressed alerts are kept (`GET …/alerts?include_suppressed=true`): the record shows what the
rules saw even when nobody was paged.

## Explainer

Queued only after the alert is stored and published. The prompt (`alert_explainer@v2`) gets the
alert, its feature values, the prior and the offset evidence as ids R1, R2 … (well, MD,
document, page). The answer is verified like a briefing: every number must appear in the
alert's evidence at the precision written (probabilities may be written as percentages), every
citation must be a listed id. One retry with the problems; then `explanation_status = 'failed'`.
A timeout (30 s) gives `timeout`; no LLM configured gives `disabled`. Calls are logged in
`llm_call` with `alert_id`.

## API

`operate` = role `wellsite_engineer` or `superintendent` ([Security](security.md#identity-and-roles)).

| Method and path | Permission | Does |
|---|---|---|
| `WS /active/{id}/stream` | any | Live channels, rig state, posteriors, alerts, feed state |
| `GET /active/{id}/alerts?hours=12&include_suppressed=false` | any | Open and recent alerts |
| `GET /active/{id}/feed` | any | Connector state and recent gaps |
| `POST /alerts/{id}/ack` | operate | Acknowledge (409 if not open) |
| `POST /alerts/{id}/feedback` | operate | `true_positive` / `false_positive` / `unsure` + note |
| `POST /active/{id}/shelves` | operate | `{hazard, reason, minutes ≤ 720}` |
| `DELETE /shelves/{id}` | operate | Unshelve |
| `GET /active/{id}/history?minutes=60&max_points=1500` | any | Recent samples in the stream's shape (the live console fills its charts from it, then appends the stream) |
| `GET /active/{id}/envelope` | any | Hookload against bit depth tripping out, tripping in and rotating (this soft-string model, the well's survey and mud, the calibrated friction factor and μ + 0.1); MW/ECD window per formation from offset kicks (pore, lower bound), losses and LOT/FIT (fracture, upper bound), each bound with its records |

**WebSocket.** Authenticate with an `Authorization: Bearer` header or, from a browser, by
sending `{"type": "auth", "token": "…"}` first (within 5 s); bad auth closes with 4401, a
wellbore outside the caller's blocks with 4404 (not found or not visible, as everywhere).
Messages:

```json
{"type": "snapshot", "feed": {"state": "live"}, "sample": {…}, "alerts": […], "stale_after_s": 6}
{"type": "sample", "ts": "…", "values": {"bit_md": 1944.2, …}, "derived": {"ecd": 1.31, …},
 "rig_state": "drilling_rotary", "formation": "Barail", "posterior": {"kick": 0.004, …}}
{"type": "alert", "event": "raise|escalate|reopen|clear|explained", "alert": {"id": 17, "hazard": "kick",
 "tier": 2, "tier_name": "warning", "posterior": 0.62, "prior": 0.0067, "lr": 243.1, "features": {…},
 "explanation_status": "pending", "explanation": null}}
{"type": "feed", "state": "live|stale|disconnected|stopped"}
```

Sample messages are throttled to two per second; alert and feed messages are never throttled.
The service publishes with PostgreSQL `NOTIFY` (one channel per wellbore); the API checks the
caller's access before it `LISTEN`s.

**Live view.** `http://localhost:5173/#/live/<wellbore id>` (after signing in) shows channels,
rig state, formation and alerts; any state other than live is shown grey with "DATA STALE" /
"DATA DISCONNECTED" ([UI](ui.md)).

## Running it

```bash
make seed                 # demo + synthetic wells
make rt-models            # train detectors (a few minutes), metrics into this page
make realtime             # WITSML simulator + real-time service for RT_WELLBORE_ID
```

Or by hand: `python -m api.realtime run --wellbore-id 7 --connector witsml --url … --uid-well …`
(`--connector wits0 --port 7000 --units '{"spp": "psi"}'`, `opcua --url opc.tcp://… --nodes
'{"hookload": "ns=2;s=HKLD"}'`, `sql --query "SELECT … WHERE ts > %(since)s"`).

Tables: `rt_feed`, `rt_gap`, `alert_shelf`, alert lifecycle columns ([Data model](data-model.md#real-time)).

In the stack the simulator serves HTTPS (`--tls-cert`, `--tls-key`) and the service checks it
against the stack CA. `RT_METRICS_PORT` serves Prometheus metrics over TLS: feed latency
(sample timestamp to stored and published, objective p95 < 10 s) and alert evaluation (sample
received to alerts stored and delivered, objective < 5 s). Each sample is traced (`rt.sample`,
5 % sampled, `alert.raise` events) and each explanation (`rt.explain` with its LLM call). At
start the service writes a `model.load` audit entry with the ruleset, detector version and
SHA-256, and the LLM model and prompt versions. Service levels, alerts and runbooks:
[Operations → Observability](operations/index.md#observability). On a rig box the same
service runs offline: [Rig Edge Pack](operations/edge.md).

## Tests

| Test | Checks |
|---|---|
| `test_alerts_fire_when_the_llm_dies_and_stale_feed_is_flagged` (database) | Synthetic well with a kick and a loss replayed at 600× through the simulator, the WITSML poller and the service; a real HTTP LLM service is killed after its first explanation. Kick and loss alerts are stored and published; later explanations are `failed`/`timeout`; every `raise` precedes its `explained`; prior × LR = posterior; one row per sample; pausing the feed gives `stale` in < 10 s and one stale gap |
| `test_websocket_streams_and_turns_stale` (database) | Snapshot, then live data, then the API alone declares the feed stale in < 10 s; another block's user is refused |
| `tests/e2e/live-stale.spec.ts` (browser) | The live view turns grey with "DATA STALE" within 10 s of the last sample |
| `test_writes_are_idempotent_per_channel` (database) | Same sample twice, two connectors at one timestamp → one merged row |
| `test_shelve_ack_feedback` (database) | Reason and 12 h limits, roles, RLS 404, acknowledge once, audit entries |
| `test_held_out_kick_raises_a_warning` | A bank trained on other wells raises a calibrated warning within 10 min of a kick's onset |
| `test_folds_never_share_a_well`, `test_calibration_is_monotone_and_lr_finite` | Group K-fold, monotone isotonic map, LR bounded, out-of-fold AUC > 0.75 |
| `test_rig_state_rules` | Every rule of the table |
| physics tests | HB Newtonian limit, yield stress raises the gradient, ECD > MW, MSE and dxc formulas, vertical soft-string = buoyed weight, friction factor recovered to ±0.005 |
| policy tests | On-delay ignores a spike; escalation, deadband, off-delay, one alert per episode, reopen within re-arm; shelving never hides critical; budget per shift resets at shift change |
| parser tests | WITS0 frames, WITSML imperial units and null values, simulator round trip |

## Limits

- Detectors are trained and evaluated on synthetic data only. Before use on a rig: replay real
  wells with known events (Volve, FORGE, operator data), retrain, and review thresholds with the
  drilling team.
- ETP 1.2 is a stub; FORGE and Volve replays are untested on the real files.
- Flow-out needs a meter in L/min; paddle percentages are not handled.
- One friction factor for cased and open hole; laminar annular flow only.
- One service process per wellbore; the API holds one `LISTEN` connection per open WebSocket.
