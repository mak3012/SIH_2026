# Canonical depth and position

Every depth, density and position in the system passes through `packages/core/src/core/canonical`
before it is stored or compared. Reports disagree about units (ft or m, ppg or sg), depth
references (KB, RT, DF, GL), geodetic datums (WGS 84, Kalianpur 1975) and formation names
("Barail", "BARAIL SST", "Brl"). The canonical layer turns them into one form, records how, and
refuses to guess.

| Module | Does |
|---|---|
| `units` | pint-based conversion, with a log entry for every conversion applied to a fact |
| `survey` | Minimum curvature; position, inclination and azimuth at any MD on the arc; MD at a TVD |
| `datum` | Depth references per wellbore; TVDSS; moving a depth between references |
| `crs` | WGS 84 from a stated or unknown datum, with pinned EPSG operations, datum confidence and an uncertainty radius |
| `ontology` | Basin → group → formation → member tree, synonyms, abbreviations; name matching where an LLM may suggest but only a rule or a person confirms |
| `position` | Formation-relative position; projecting an offset event onto the active well |
| `reanchor` | Re-projecting a well's hazards when a geologist picks a top |

Stored results: `source_ref.conversions`, `wellbore.datum_type` / `datum_elevation_m`,
`well.source_crs` / `datum_confidence` / `position_uncertainty_m`, `basin`, `formation.rank` /
`abbreviations` / `expected_tvdss_*`, `name_match`, `hazard_profile.rel_top` / `rel_base`
(migration `0010_canonical`, see [Data model](data-model.md)).

## Units

Canonical: length **m**, density **sg**, pressure gradient as its **sg equivalent**, volume
**bbl** where the column says so. The conventions are explicit in code, not library defaults:

| Unit | Definition used |
|---|---|
| ft | international foot, 0.3048 m exactly (US survey foot is `survey_foot`, 2 ppm longer) |
| sg | 1000 kg/m³ (oilfield convention; pure water at 4 °C is 999.97 kg/m³) |
| ppg | pound per US gallon: 1 sg = 8.3454 ppg |
| pcf | pound per cubic foot: 1 sg = 62.428 pcf |
| psi/ft, kPa/m | gradients; to a density through standard gravity 9.80665 m/s²: 1 sg = 0.43353 psi/ft |
| bbl | oil barrel, 42 US gal = 0.158987 m³ |

**Worked example.** A WCR prints TD `9,851 ft`, a DDR prints mud weight `10.2 ppg`, a LOT sheet
prints `0.53 psi/ft`:

| Printed | Canonical | Logged step |
|---|---|---|
| 9,851 ft | 3,002.585 m | `ft -> m`, factor 0.3048 |
| 10.2 ppg | 1.2222 sg | `ppg -> SG`, factor 0.11983 |
| 0.53 psi/ft | 1.2225 sg | `psi/ft -> SG via g0 = 9.80665 m/s2` |

Each step is stored with the fact, in `source_ref.conversions`:

```json
[{"field": "td_md", "value_in": 9851, "unit_in": "ft", "value_out": 3002.5848,
  "unit_out": "meter", "factor": 0.3048, "via": "ft -> m", "library": "pint 0.26.1"}]
```

Identity conversions (a value already in metres) are not logged. A density printed without a
unit is taken as sg only if it is below 3 (no other density unit gives drilling values that
small); otherwise it is refused as unreadable and goes to review.

## Minimum curvature

Between two survey stations the hole is taken to be a circular arc in one plane:

```
β   = arccos(cos(I₂−I₁) − sin I₁ sin I₂ (1 − cos(A₂−A₁)))      dogleg
RF  = (2/β) tan(β/2)                                            ratio factor (1 if β = 0)
ΔN  = ΔMD/2 · RF · (sin I₁ cos A₁ + sin I₂ cos A₂)
ΔE  = ΔMD/2 · RF · (sin I₁ sin A₁ + sin I₂ sin A₂)
ΔTVD= ΔMD/2 · RF · (cos I₁ + cos I₂)
```

**Worked example** (published: [drillingformulas.com, Minimum Curvature Method](https://www.drillingformulas.com/minimum-curvature-method/)):
3,500 ft, 15°, 20° to 3,600 ft, 25°, 45°.

| | Published | `core.canonical.survey` |
|---|---|---|
| Dogleg β | 0.22605 rad = 12.95° | 0.22605 rad = 12.95° |
| RF | 1.00408 (misprint, see below) | 1.00428 |
| ΔN | 27.22 ft | 27.22 ft |
| ΔE | 19.45 ft | 19.45 ft |
| ΔTVD | 94.01 ft | 94.01 ft |

The page's RF is a misprint: its own ΔTVD requires 1.00428, because ΔTVD = 50 · RF · (cos 15° +
cos 25°) = 93.612 · RF, and 93.612 × 1.00408 = 93.99, not 94.01.

### Position at any MD

Interpolating *between* stations follows the same arc: the tangent is interpolated spherically
(`sin((1−f)β)/sin β · t₁ + sin(fβ)/sin β · t₂`), then minimum curvature runs to that point. Linear
interpolation of TVD cuts the corner of the arc.

**Worked example.** Stations at 1,000 m (0°, 90°) and 1,300 m (60°, 90°); where is MD 1,150 m?

| | TVD | East | Inclination |
|---|---|---|---|
| Station 1,300 m | 1,248.10 | 143.24 | 60° |
| **Arc, MD 1,150 m** | **1,143.24** | **38.38** | **30°** |
| Linear between stations | 1,124.05 | – | – |

Linear interpolation puts the point 19 m too shallow. `md_at_tvd` inverts `tvd_at` by bisection
on the arc (to 1 µm).

## Depth datums and TVDSS

A depth is meaningless without its reference. Every wellbore stores its own **datum type** (KB,
RT, DF, GL, MSL) and **elevation above MSL** (a sidetrack or workover can use a different rig).

```
TVDSS = TVD − datum elevation                  (positive down, below mean sea level)
depth below B = depth below A − elev(A) + elev(B)
```

**Worked example.** A top is reported at 2,912 m TVD below KB, KB 112.0 m AMSL. The drilling
rig's RT was 110.0 m AMSL.

| | Value |
|---|---|
| TVDSS | 2,912 − 112 = **2,800.0 m** |
| Same point below RT | 2,912 − 112 + 110 = **2,910.0 m** |
| TVDSS computed from RT | 2,910 − 110 = **2,800.0 m** (the same point, as it must be) |

Mixing the two references without rebasing puts the top 2 m off; a DF/GL mix-up is usually 5–10
m, enough to put an event on the wrong side of a thin formation boundary.

## Coordinates, datums and confidence

Positions are stored in WGS 84 with where they came from:

| Field | Meaning |
|---|---|
| `source_crs` | CRS the report used, e.g. EPSG:4146 (Kalianpur 1975) |
| `datum_confidence` | **known**: the report states the datum · **inferred**: context suggests it (vintage, operator practice) · **unknown**: nothing says |
| `position_uncertainty_m` | Radius that covers the true position: transformation accuracy, plus the spread of the other possible datums when not known |

Transformations are **pinned by EPSG operation code**, never "best available" (which depends on
the machine's PROJ version and grids):

| Source CRS | Operation | Accuracy |
|---|---|---|
| EPSG:4146 Kalianpur 1975 | EPSG:1156, geocentric translation (295, 736, 257) m, India mainland | 22 m |
| EPSG:4144 Kalianpur 1937 | EPSG:1155, geocentric translation (282, 726, 254) m, Bangladesh; used outside its area, +10 m | 28 m |

**Worked example.** A 1988 WCR prints 27°06'00"N 94°54'00"E.

| Datum situation | WGS 84 position | Radius | Why |
|---|---|---|---|
| Report says "Kalianpur 1975" (known) | 27.100495 N, 94.896402 E | **22 m** | accuracy of EPSG:1156 |
| No datum printed, pre-2000 Indian report (inferred Kalianpur 1975) | 27.100495 N, 94.896402 E | **389 m** | the other candidates are up to 361 m away, plus 28 m |
| No datum and no context (unknown) | 27.100000 N, 94.900000 E (as WGS 84) | **389 m** | every candidate is possible |

The same printed numbers are **361 m apart** depending on the datum: a distance larger than many
"offset well within 500 m" questions. The pipeline stores well positions only from reports that
state the datum (rule `R-DATUM`), so extracted wells are `known`; `inferred` and `unknown` are
for manual entry and legacy imports.

Survey offsets (N, E in metres, true-north azimuths) are placed on the ellipsoid along the
geodesic (`offset_to_lonlat`, pyproj `Geod`), not with a flat-earth approximation. Grid-north
surveys must be rotated by the grid convergence first.

## Formation ontology and name matching

The ontology is a tree: **basin → sub-basin → group → formation → member**, each unit with
synonyms (other spellings, older names), abbreviations and, for formations, a typical TVDSS range
in the basin. Names are normalised before comparison: `Barail Fm.`, `BARAIL SST` and `barail` are
all `BARAIL` (lithology and rank words are dropped); when a group and a formation share a name,
the one the text names wins ("Barail Group" → the group, "Barail" → the formation).

**An LLM may suggest a match; only a rule or a person confirms it.**

| Check | Rule |
|---|---|
| In the ontology | the candidate exists and lies in the expected basin (else **rejected**) |
| Name close | exact, synonym or abbreviation; or edit distance (Damerau–Levenshtein) ≤ max(1, length / 6) |
| Depth plausible | if a depth is known and the formation has a range: inside it, ± 150 m |

All pass → **confirmed** (`confirmed_by = 'rule'`). Otherwise → **needs_human**; a reviewer's
confirmation is recorded with their name. Every decision, whoever proposed it, is stored in
`name_match`.

**Worked examples** (Upper Assam ontology from the tests):

| Printed | Depth (TVDSS) | Suggested by | Decision | Why |
|---|---|---|---|---|
| `Barail Fm.` | 2,600 m | rule | confirmed | exact after normalisation |
| `BRL` | 2,600 m | rule | confirmed | abbreviation |
| `Barial` | 2,600 m | rule | confirmed | edit distance 1 (transposition), depth in range |
| `Barial` | 900 m | rule | **needs_human** | name close, but the Barail is not at 900 m |
| `Upper sand unit` | 2,000 m | LLM → Tipam | **needs_human** | the LLM's guess fails the name rule |
| `Bokabil` | – | rule | **rejected** | not in the Upper Assam basin |

Well names follow the same pattern: `SA-01`, `sa 1` and `SA#001` normalise to `SA-1`; a match
needs edit distance ≤ 1, the same block, and any depth quoted from it ≤ the well's TD.

## Formation-relative position

Depth alone does not say which rock an event was in. Formations dip and thin between wells, so
a hazard seen at some TVDSS in an offset well must be carried to the active well **by formation**,
not by depth:

```
rel = (TVDSS − top) / (base − top)          0 at the top of the formation, 1 at its base
projected TVDSS = top′ + rel · (base′ − top′)   using the active well's top′ and base′
```

<figure>
<svg width="980" height="560" viewBox="0 0 980 560" role="img" aria-labelledby="sdwr-title sdwr-desc" style="max-width:100%;height:auto" xmlns="http://www.w3.org/2000/svg">
  <title id="sdwr-title">Same depth, wrong rock versus same formation</title>
  <desc id="sdwr-desc">Two wells with dipping formations. An event 10% into the Barail in the offset well at 2,665 m TVDSS lands in the Tipam if carried at the same depth to the active well, and at 2,832 m TVDSS, 10% into the Barail, if carried formation-relatively.</desc>
  <polygon points="60,74.8 740,132.7 740,310.9 60,249.1" fill="#d9b36c" fill-opacity="0.45"/>
  <polygon points="60,249.1 740,310.9 740,495.7 60,406.8" fill="#8c8c8c" fill-opacity="0.45"/>
  <polygon points="60,406.8 740,495.7 740,522.5 60,522.5" fill="#6f8fae" fill-opacity="0.45"/>
  <line x1="60" y1="74.8" x2="740" y2="132.7" stroke="currentColor" stroke-width="1"/>
  <line x1="60" y1="249.1" x2="740" y2="310.9" stroke="currentColor" stroke-width="1"/>
  <line x1="60" y1="406.8" x2="740" y2="495.7" stroke="currentColor" stroke-width="1"/>
  <g fill="currentColor" font-family="sans-serif" font-size="14">
    <text x="400" y="200" text-anchor="middle" font-weight="bold">Tipam</text>
    <text x="400" y="375" text-anchor="middle" font-weight="bold">Barail</text>
    <text x="400" y="505" text-anchor="middle" font-weight="bold">Kopili</text>
    <text x="180" y="30" text-anchor="middle">Offset well</text>
    <text x="620" y="30" text-anchor="middle">Active well</text>
    <text x="10" y="114" font-size="11">2,000</text>
    <text x="10" y="239" font-size="11">2,500</text>
    <text x="10" y="364" font-size="11">3,000</text>
    <text x="10" y="489" font-size="11">3,500</text>
    <text x="10" y="550" font-size="11">TVDSS, m</text>
  </g>
  <line x1="180" y1="40" x2="180" y2="522" stroke="currentColor" stroke-width="3"/>
  <line x1="620" y1="40" x2="620" y2="522" stroke="currentColor" stroke-width="3"/>
  <line x1="180" y1="276.25" x2="620" y2="276.25" stroke="#c62828" stroke-width="2" stroke-dasharray="6 5"/>
  <circle cx="620" cy="276.25" r="7" fill="none" stroke="#c62828" stroke-width="2.5"/>
  <line x1="627" y1="276.25" x2="752" y2="262" stroke="#c62828" stroke-width="1.5"/>
  <text x="758" y="258" fill="#c62828" font-family="sans-serif" font-size="14" font-weight="bold">same depth: 2,665 m</text>
  <text x="758" y="276" fill="#c62828" font-family="sans-serif" font-size="14">in the Tipam: wrong rock</text>
  <path d="M180,276.25 C 330,285 470,305 620,318" fill="none" stroke="#1a7f37" stroke-width="2.5"/>
  <circle cx="620" cy="318" r="7" fill="#1a7f37"/>
  <line x1="627" y1="318" x2="752" y2="334" stroke="#1a7f37" stroke-width="1.5"/>
  <text x="758" y="334" fill="#1a7f37" font-family="sans-serif" font-size="14" font-weight="bold">same formation: 2,832 m</text>
  <text x="758" y="352" fill="#1a7f37" font-family="sans-serif" font-size="14">10 % into the Barail</text>
  <circle cx="180" cy="276.25" r="7" fill="#c62828"/>
  <text x="190" y="300" fill="currentColor" font-family="sans-serif" font-size="13">losses at 2,665 m, rel 0.10</text>
</svg>
<figcaption>Barail losses in an offset well, carried to the active well two ways.</figcaption>
</figure>

**Worked example** (the figure).

| | Offset well | Active well |
|---|---|---|
| Tipam top | 1,900 m | 2,050 m |
| Barail top | 2,600 m | 2,760 m |
| Kopili top (= Barail base) | 3,250 m | 3,480 m |
| Losses | 2,665 m TVDSS | ? |

- rel in the offset well = (2,665 − 2,600) / (3,250 − 2,600) = **0.10**
- **Same depth** (2,665 m) in the active well is between the Tipam top (2,050) and the Barail top
  (2,760): **in the Tipam**, 95 m above the Barail — the wrong rock, and a warning placed there
  would sound 167 m before the bit reaches the depth where the losses actually belong.
- **Same formation**: 2,760 + 0.10 × (3,480 − 2,760) = **2,832 m TVDSS**, 10 % into the Barail.
  With the active well's survey and datum, `project()` also returns the MD.

Edge cases are explicit, never silent: `below_top` when a formation's base is unknown in either
well (the deepest formation reached: the distance below the top is kept and a note says so);
`absent` when the formation has no top in the active well (not reached, eroded, or not yet
picked); `above_first_top` when the event is above the offset well's first known top.

## Live top re-anchoring

Hazards are stored formation-relatively (`hazard_profile.formation_id`, `rel_top`, `rel_base`)
with their absolute depths as a cache. When a geologist picks a top, every hazard of that well
is re-projected against the new tops and survey.

**Worked example.** The Barail loss hazard in the active well spans rel 0.00–0.10: 2,760–2,832 m
TVDSS from the prognosed tops. While drilling, the geologist picks the Barail top at 2,800 m
TVDSS (40 m deeper). Re-anchoring moves the hazard to **2,800–2,868 m** (0.10 × (3,480 − 2,800)
= 68 m thick now) and recomputes its MD range along the survey; the audit log records the pick
and how many hazards moved.

In the system:

- `pipelines.reanchor.pick_top(conn, wellbore_id, formation_id, md, picked_by)` is the call a
  review screen or the API makes. In one transaction it computes TVD (arc interpolation) and
  TVDSS (the wellbore's datum), stores the top with a `source_ref` that cites the well's "Top
  picks" document with the geologist as reviewer, re-projects the hazards and writes an audit
  entry.
- The Dagster sensor `top_picks` finds wellbores with a human pick newer than their hazards'
  last re-projection (picks inserted another way) and runs `reanchored_hazards`.
- A human pick wins over extracted tops of the same formation; hazards whose formation has no
  top, or whose base is unknown, keep their depths and are reported.

## Tests

`packages/core/tests` (hypothesis property tests where marked):

| Test | Checks |
|---|---|
| `test_published_example` | The drillingformulas.com example to the published precision |
| `test_minimum_curvature_is_exact_on_circular_arcs` (property) | Random exact circular arcs (any start direction, bend plane, curvature up to ~14°/30 m, uneven station spacing): every station position within 1 µm |
| `test_interpolation_follows_the_arc` (property) | Position, inclination and azimuth at a random MD equal the exact arc |
| `test_md_at_tvd_inverts_tvd_at` (property) | Round trip MD → TVD → MD within 10 µm |
| `test_round_trip` (property) | ft/m/in, sg/ppg/kg/m³/pcf/g/cc, psi/ft/kPa/m ↔ densities, bbl/m³: A→B→A within 1e-12 |
| `test_reference_values` | 0.3048 m, 0.158987 m³, 8.3454 ppg, 0.43353 psi/ft, 62.428 pcf, 10 ppg = 0.5195 psi/ft |
| `test_pinned_operation_parameters_are_epsg_1156` | The pinned operation is EPSG:1156 with (295, 736, 257) m, accuracy 22 m |
| `test_pinned_transform_matches_independent_helmert` (property) | Control: pyproj's EPSG:1156 and an independent geocentric-translation implementation (`core.geodesy`, no pyproj) agree to 1e-9° anywhere in India; inverse within 1e-7° (the 2D method drops height) |
| `test_projection_control_point_snyder` | Control point: Snyder (1987) Transverse Mercator example, 40°30'N 73°30'W on Clarke 1866 → 127,106.5 / 4,484,124.4 m, reproduced to 0.1 m |
| `test_datum_confidence_and_uncertainty` | known = 22 m; inferred/unknown radius exceeds the 361 m datum spread |
| ontology / position / re-anchor tests | The worked examples above |

`pipelines/tests/test_reanchor.py` runs `pick_top` against a database; `test_store.py` checks that
stored wells carry datum confidence, wellbores their datum, formation names a `name_match`
decision, and converted depths their conversion log.
