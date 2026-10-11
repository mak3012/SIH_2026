# Intelligence

Two parts, kept separate by [ADR-0002](adr/0002-rules-decide-llm-explains.md):

- **Rules and statistics decide**: analogue scores, hazard probabilities, program findings.
  Deterministic, versioned, unit-tested, reproducible from the database.
- **The LLM explains**: it drafts the briefing text, and rules check every number and citation in
  it before anyone sees it.

Code: `apps/api/src/api/intelligence/` — `analogues.py`, `hazards.py`, `linter.py`, `stats.py`,
`repo.py` (database reads), `routes.py` (HTTP). Endpoints are listed in [API](api.md). All
queries run under the caller's row-level security, so offsets and evidence outside their licence
blocks never enter a number.

Depths are metres MD unless stated; TVDSS as in [Canonical depth](canonical-depth.md);
densities in sg.

!!! note "UI status"
    The well page (`#/wells/<id>`, [UI](ui.md)) shows offset selection, the hazard roadmap and
    the lookahead as described in the "What the UI shows" sections. The program linter and
    briefings have no page yet: use the API (`POST /programs/lint`, `/briefings/{wellbore}`).

## 1. Analogue (offset) wells

### Which wells are candidates

A well is a candidate when **both**:

- its surface location is within the **surface radius** of the active well (default 10 km;
  PostGIS `ST_DWithin` on `geography`), and
- the point where its trajectory crosses the **target formation's top** is within the
  **subsurface radius** of the active well's crossing point (default 5 km).

The crossing point is interpolated in PostGIS between the two survey stations around the top's
MD (`ST_LineInterpolatePoint` on the segment). Why both: wells drilled from one pad can be
kilometres apart at the target, and wells far apart at surface can reach the same spot. A well
with no crossing point (no top or no survey) is listed but not auto-included; the user can pin it.

### Score

Each candidate gets a **weighted mean of factor scores**, every factor in [0, 1]:

```
score = Σ_f w_f · s_f / Σ_f w_f
```

| Factor | Default weight | Score s_f | Parameter |
|---|---|---|---|
| Distance at formation | 3 | exp(−d / 3000 m), d = distance between crossing points | 3,000 m |
| Same fault block | 2 | 1 same, 0 different | — |
| Top-depth delta | 2 | exp(−\|ΔTVDSS\| / 100 m) of the target top | 100 m |
| Hole / casing design | 1 | Jaccard overlap of casing sizes | — |
| Mud system | 1 | 1 if the planned mud type was used in the offset, else 0 | — |
| Vintage | 0.5 | exp(−\|Δ spud year\| / 15 yr) | 15 yr |
| Data quality | 1 | mean of: has survey, has tops, problems documented, mean fact confidence | — |

**Unknown is not zero.** A factor that cannot be evaluated (no fault-block interpretation, no
casing record) scores **0.5** and is flagged `unknown`, so missing data neither lifts nor sinks a
well silently. The UI shows the flag.

**Worked example** (numbers from the code): offset OFF-2, crossing points 1,850 m apart, same
fault block, Barail top 18 m shallower, identical casing program, has used WBM, spudded 2011
(active 2026), data quality 0.83.

| Factor | Raw | Score | Weight | Contribution |
|---|---|---|---|---|
| Distance at formation | 1,850 m | exp(−1850/3000) = 0.540 | 3 | 0.154 |
| Fault block | FB-N | 1.000 | 2 | 0.190 |
| Top-depth delta | 18 m | exp(−18/100) = 0.835 | 2 | 0.159 |
| Design | 13-3/8, 9-5/8, 7 | 1.000 | 1 | 0.095 |
| Mud system | wbm, obm | 1.000 | 1 | 0.095 |
| Vintage | 2011 | exp(−15/15) = 0.368 | 0.5 | 0.018 |
| Data quality | 0.83 | 0.830 | 1 | 0.079 |
| **Score** | | | 10.5 | **0.791** |

The contributions add up to the score; that is what "transparent" means here.

### Pins, exclusions, weights, learning-to-rank

`POST /offsets/select` takes the user's weights (any subset; 0 switches a factor off), pinned
wells (always included, even outside the radii) and excluded wells (never included). Inclusion:
pinned wells, then the best-ranked wells inside both radii up to `max_offsets` (10).

Every call stores the whole picture: `offset_selection` (radii, weights, pins, exclusions,
scales, who, when) and one `offset_selection_item` per candidate (raw features, factor scores,
total, rank, pinned / excluded / included). These rows are the training data for a future
learning-to-rank model: features plus what an engineer actually chose. Nothing is learned yet;
the scores above are the rule.

### What the UI shows

| Number | Meaning |
|---|---|
| Score 0.79 | Weighted mean above; 1 = identical in every factor that can be evaluated |
| Contribution | The factor's share of the score (w·s / Σw) |
| `unknown` badge | Factor not evaluable; counted as 0.5 |
| Surface / at-formation distance | Metres, geodesic |
| Rank | Order after pins; pinned wells first |

## 2. Hazard roadmap

For every formation along the planned trajectory and every hazard type (loss, kick, stuck
pipe, tight hole, pack-off, torque spike, cement issue, washout, fishing), each selected offset is
classified:

| Status | Meaning | Counts as |
|---|---|---|
| `event` | had that hazard in that formation | evidence, y = 1 |
| `none` | drilled the formation, problems documented there (≥ 80 % of its interval), no such event | evidence, y = 0 |
| `unknown` | drilled it, but problems not documented there | **not evidence** |
| `not_reached` | never reached the formation | not evidence |

**Unknown is kept apart from none.** Documentation comes from `event_documentation`: a WCR
drilling-problems table (even one that says "no significant problems") documents the whole well;
a daily report documents the interval drilled that day. A well with no such record for the
formation is `unknown`, however clean it looks. The UI shows all four counts.

### Posterior

Similarity-weighted Beta-Binomial. With weights w_j = the offsets' analogue scores, over the
offsets with evidence:

```
n_eff = (Σ w_j)² / Σ w_j²                 Kish effective number of wells
w'_j  = w_j · n_eff / Σ w_j                weights rescaled so they sum to n_eff
α = a0 + Σ w'_j y_j        β = b0 + Σ w'_j (1 − y_j)
mean = α / (α + β)         90 % interval = [Beta⁻¹(0.05; α, β), Beta⁻¹(0.95; α, β)]
```

Rescaling to n_eff stops twenty weakly similar wells from claiming the certainty of twenty
identical ones: weights [1, 1, 0.05 × 18] are worth 4.1 wells, not 20.

**Prior.** Centred on the basin rate m = (k + 0.5) / (n + 1), where k of n visible wells with
the formation documented had the hazard there, with strength 2 wells: a0 = 2m, b0 = 2(1 − m).
Without basin data: Jeffreys, Beta(0.5, 0.5). Basin wells count as documented only when a single
record covers the whole formation interval (a slightly stricter test than the 80 % used per
offset); the prior's weight of 2 wells keeps the difference small.

**Worked example** — Barail losses, six offsets:

| Offset | Weight | Status | y |
|---|---|---|---|
| A | 0.82 | event | 1 |
| B | 0.74 | event | 1 |
| C | 0.69 | none | 0 |
| D | 0.55 | none | 0 |
| E | 0.41 | none | 0 |
| F | 0.30 | event | 1 |

Basin: 9 of 25 documented wells had Barail losses → m = 9.5 / 26 = 0.365, prior Beta(0.731,
1.269). Σw = 3.51, n_eff = 5.46, scale 1.556. α = 0.731 + 1.556 × (0.82 + 0.74 + 0.30) = 3.624;
β = 1.269 + 1.556 × (0.69 + 0.55 + 0.41) = 3.836.
**Mean 0.486, 90 % interval 0.205–0.771, n_eff 5.5.** (Unweighted with a Jeffreys prior the
same six wells give 0.500, 0.209–0.791.)

### The rest of a roadmap cell

| Field | Meaning |
|---|---|
| `severity` | Share of the weighted offset events at severity 1 … 5 |
| `rel_position` | Weighted 10th, 50th, 90th percentile of where the events sat inside the formation (0 = top, 1 = base) |
| `band_md`, `band_tvdss` | That q10–q90 range placed in the **active** well's formation interval (formation-relative, see [Canonical depth](canonical-depth.md#formation-relative-position)) |
| `mitigations` | Actions taken against those events: times used, times recorded as effective |
| `evidence` | Every offset event behind the cell: well, MD, severity, weight, document and page |
| `evidence_state` | `no_evidence` when no offset has event or none: the numbers are the prior alone |

### What the UI shows

- **"Barail · loss 49 % (21–77 %), n_eff 5.5"**: posterior mean, 90 % interval, effective
  number of wells. A wide interval means "we do not know well"; it is not a softer "yes".
- **"3 event · 3 none · 1 unknown · 0 not reached"**: raw counts. Unknowns are data gaps to
  close, not reassurance.
- **Band 2,712–2,780 m MD**: where in *this* well the offsets' events would sit.
- A cell with **no evidence** is shown hatched with the prior, never as 0 %.

## 3. Lookahead (ETA to hazard bands)

`GET /active/{id}/lookahead` places the bit and the rate:

- **Current depth**: latest real-time hole depth (`rt_measurement.hole_md`, else `bit_md`).
- **Rolling ROP**: net progress over the last `window_h` hours (default 12): (MD now − MD then) /
  hours. This includes connections, surveys and trips, so it predicts arrival, not on-bottom
  speed. Without real-time data: the last three daily reports (`mud_record` depths).
- For every band with posterior mean ≥ `min_mean` (default 0.10): `passed`, `in band`, or
  `ahead` with distance (m) and **ETA = distance / rate** (hours, and a timestamp).

Illustration: bit at 1,944 m, 12 m/h over the last 12 h, Barail loss band from 2,712 m →
768 m ahead, **64 h**.

## 4. Program linter

`POST /programs/lint` takes a drilling program (metres MD, sg, inches):

```json
{"wellbore_id": 42, "td_md": 3700, "ecd_margin_sg": 0.05,
 "sections": [{"name": "12-1/4 in intermediate", "md_top": 520, "md_base": 2700,
               "hole_in": 12.25, "casing_od_in": 9.625, "mud_type": "wbm",
               "mw_sg_min": 1.15, "mw_sg_max": 1.25}],
 "cement": [{"casing_od_in": 7.0, "toc_md": 2650, "slurry_density_sg": 1.9}]}
```

**Rules** (ruleset `lint@1.0.0`). Thresholds: block at posterior ≥ 0.30, warn at ≥ 0.10;
fracture margin 0.03 sg; kick margin 0.05 sg; cement overlap 150 m. Planned ECD = `mw_sg_max` +
`ecd_margin_sg`.

| Rule | Severity | Fires when | Evidence cited |
|---|---|---|---|
| L-STD-CASING | block | casing OD not an API 5CT size | — |
| L-STD-HOLE | warn | hole size not a common bit size | — |
| L-CLEARANCE | block | hole not larger than the casing run in it | — |
| L-CONTIGUOUS | block | a section does not start where the previous ended | — |
| L-CASING-ORDER | block | a string does not fit inside the previous one | — |
| L-TD | block | sections do not end at TD | — |
| L-MW-FRAC | block | planned ECD > weakest offset LOT/FIT in the formation of the previous shoe − 0.03 sg | that test |
| L-SEAT-LOSS | block (p ≥ 0.30) / warn (≥ 0.10) | open-hole ECD ≥ the lowest ECD at which offsets lost returns in a formation the section crosses | those losses |
| L-MW-KICK | block / warn | minimum MW < highest MW at which offsets kicked in that formation + 0.05 sg | that kick |
| L-MUD-STUCK | warn | WBM through a formation with stuck-pipe posterior ≥ 0.20 | stuck-pipe events |
| L-CMT-DENSITY | block | slurry density > weakest offset LOT/FIT at the previous shoe − 0.03 sg | that test |
| L-CMT-TOC | warn | top of cement less than 150 m above the previous shoe | — |
| L-CMT-CASING | block | cement job for a casing the program does not run | — |
| L-DATA-GAP | info | more offsets `unknown` than with evidence in a crossed formation | — |

Every finding carries its **numbers** (the values it compared) and **evidence ids** `E1, E2, …`
resolving to offset well, formation, MD, value, and the source document and page. Findings are
stored in `lint_run` with the roadmap they used.

## 5. Briefing

`POST /briefings/{wellbore}` turns a lint run into a plain-language briefing:

1. The LLM (`briefing@v2` prompt, guided JSON: title + paragraphs, each with `citations`) drafts
   from the findings, the evidence list and the top of the roadmap. Temperature 0, logged in
   `llm_call` like every model call.
2. **Rules verify the draft:**
    - every number in the text must equal a number in the findings, the evidence or the roadmap
      at the precision written (so "1.36 sg" matches 1.3612; "40 %" matches a probability
      0.4); well names, section names, pipe sizes and document titles are not read as numbers;
    - every paragraph that states an offset number must cite evidence (numbers that come only
      from the program itself, such as a planned top of cement, need no citation);
    - every cited id must exist.
3. A failing draft is retried once with the list of problems. If it fails again, or no LLM is
   configured, the briefing is the **rule-generated text** (one paragraph per finding), which
   passes the same checks. `status` says which: `verified` or `fallback`.
4. A **PDF** (reportlab) holds the text with citations, the findings table, the evidence table
   (well, formation, MD, value, document, page) and a footer with ruleset, model and time.
   Download: `GET /briefings/{wellbore}/{id}.pdf`.

## Parameters in one place

| Parameter | Value | Where |
|---|---|---|
| Surface / subsurface radius | 10 km / 5 km | request (`/offsets/select`) |
| Distance, top-delta, vintage scales | 3,000 m, 100 m, 15 yr | `analogues.SCALES` |
| Factor weights | table in §1 | request or `analogues.DEFAULT_WEIGHTS` |
| Unknown factor score | 0.5 | `analogues.UNKNOWN_SCORE` |
| Documentation coverage for "none" | 80 % of the formation interval | `hazards.COVERAGE_MIN` |
| Prior strength | 2 wells | `hazards.PRIOR_STRENGTH` |
| Interval | 90 % (quantiles 0.05, 0.95) | `hazards.posterior` |
| Lookahead window, band threshold | 12 h, mean ≥ 0.10 | request (`/lookahead`) |
| Lint thresholds and margins | §4 | `linter` constants |

## Tests

| Test | Checks |
|---|---|
| `test_roadmap_recovers_seeded_probabilities` | 300 synthetic wells (5 seeds × 60) drawn with the per-formation probabilities of `stratigraphy.toml`: **73 of 80** (91 %) 90 % intervals contain the seeded probability; none falls outside a 99.9 % interval; zero-probability cells stay below 3 %; formations below every TD come out `no_evidence` |
| `test_jeffreys_interval_coverage_by_simulation` | Coverage of the 90 % interval in 400 simulated 30-well sets at p = 0.05, 0.2, 0.5 lies in 84–96 % |
| `test_beta_cdf_matches_binomial_identity` (property) | The Beta CDF equals the exact binomial tail for integer parameters |
| `test_unknown_is_not_none` | Undocumented and partly documented offsets are `unknown` and are not evidence |
| `test_weights_rescale_to_n_eff` | Twenty wells with small weights count as their Kish n_eff |
| analogue tests | Contributions sum to the score; unknown factors 0.5; pins, exclusions, radii |
| linter tests | Each rule fires on a crafted program; findings cite the right evidence |
| verification tests | Invented numbers, uncited offset numbers and unknown citations are caught; a failing draft is retried with feedback, then replaced by rule text; PDF renders |
| `test_intelligence_api.py` (database) | Every endpoint end to end on the demo wells plus a planned well; RLS hides wells and briefings from other blocks; only planners select offsets |
