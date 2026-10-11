# Data

| Kind | Where | Source |
|---|---|---|
| Scanned reports (WCR, DDR) | S3 object store, facts in Postgres | Operator archives |
| Surveys and trajectories | Postgres (PostGIS) | Survey files, WCRs |
| Real-time channels | Postgres (TimescaleDB) | eRTMAC / WITSML |
| Embeddings | Postgres (pgvector) | Derived from facts and passages |

Raw files go under `data/raw/`, which is git-ignored. Never commit operator data.

Tables, provenance rules, access control and example queries: [Data model](data-model.md).

## Public sources

Downloaders live in `data/` and share `data/fetch.py`: downloads resume from `<file>.part` with
HTTP Range requests, retry with back-off, record sha256 and size in `data/raw/<source>/manifest.json`,
and skip files already present with a matching checksum. Static files (PDFs, zips, fonts) are also
pinned in `data/checksums.lock.json`: commit it after the first run, and later downloads that do
not match are rejected. Each script prints its licence note before downloading and writes
`data/raw/<source>/register.json` listing the documents to load into the object store + Postgres.

| Script | Source | What | Licence |
|---|---|---|---|
| `download_sodir.py` | [Sodir FactPages](https://factpages.sodir.no/) CSV exports | Wellbores (`wellbore_all_long`), formation tops, casing + LOT/FIT, document metadata; a deterministic sample (`--sample 10`) of scanned completion-report PDFs | NLOD 2.0 for FactPages data; PDFs are licensee reports published by Sodir, check terms before redistribution |
| `download_forge.py` | [Utah FORGE 16A(78)-32](https://gdr.openei.org/submissions/1283) on the DOE GDR | Daily reports (zip, PDFs extracted), surveys, mud log, Pason data, [drilling summary](https://gdr.openei.org/submissions/1296); `--with-timeseries` adds the 10 s CSV (113 MB) | CC BY 4.0 |
| `download_volve.py` | [Equinor Volve](https://www.equinor.com/energy/volve-data-sharing) | Prefixes `Well_technical_data/`, `Reports/` by default, capped by `--max-gb 2` | Equinor Open Data Licence; needs personal acceptance |

Notes:

- **Sodir tables change daily.** They are stored as dated snapshots in
  `data/raw/sodir/tables/YYYY-MM-DD/`, checksummed but not pinned; the latest snapshot is reused
  unless `--refresh`.
- **Volve cannot be fetched anonymously.** Accept the licence through Equinor's access flow, then
  set `VOLVE_SAS_URL` (container URL with SAS token) or `VOLVE_DIR` (a folder you downloaded).
  Without either the script prints instructions and exits 0.
- **FORGE is geothermal**, not oil and gas. It is included because it is a highly deviated well
  with complete, openly licensed daily reports and surveys.
- A pinned file that changes upstream fails with `ChecksumError`. Check the change, then delete
  its entry from `checksums.lock.json` to accept the new version.

## Public wells (live, Sodir FactPages)

The map's **All wells (live)** view shows every Norwegian exploration wellbore registered with
the Norwegian Offshore Directorate (about 2,200, newest first), next to the 30 synthetic Upper
Assam wells. Real Indian well locations (DGH National Data Repository) are licensed, not public,
so the live layer uses the open Norwegian register.

| | |
|---|---|
| What | `pipelines/sodir.py`, Dagster asset `public_wells` in group `public_wells` |
| When | Schedule `public_wells_job_schedule`, daily 05:00 UTC, on by default (`SODIR_SYNC=1`); now: `make public-wells` / `.\demo.ps1 public-wells` (`make demo` runs it once) |
| Wells | Table `wellbore_exploration_all` upserted into `public_well` (location, purpose, status, content, operator, licence, field, spud and completion dates, depths, FactPage and press-release links). `first_seen` marks wells this system saw for the first time; the map flags those and wells spudded in the last 30 days as **NEW** |
| Reports | Completion reports (`*_COMPLETION_REPORT.pdf`, not logs) of the `SODIR_REPORTS_PER_RUN` (default 3) most recently spudded wellbores not checked yet, up to 60 MB each, are stored in the object store and registered as documents (block `NO-SODIR`, `source = sodir`). The `new_documents` sensor then runs them through extraction like any other document |
| API | `GET /public-wells`, `GET /public-wells/status` (row-level security: block `NO-SODIR`, which every dev user and every self-registered viewer holds) |
| Network | Outbound HTTPS to `factpages.sodir.no` from the `dagster` container (laptop: the dev overlay's `outside` network). At a site with no route out set `SODIR_SYNC=0`, or allow that one host in the egress firewall |
| Licence | NLOD 2.0. Attribution, shown under the map: "Contains data from the Norwegian Offshore Directorate (Sodir)" |

Tests: `pipelines/tests/test_sodir.py` (parsing), `tests/e2e/stack.spec.ts` (map against the stack).

## `make seed`

One command fills `data/raw/` and registers every document:

1. `python -m api.seed` (api container): 3 demo formations and 5 demo wells as database facts,
   used by the example queries (see [Demo seed](data-model.md#demo-seed)).
2. `python seed.py` in the one-shot **`datatools`** container (image with espeak-ng; `./data`
   is mounted, so everything lands in `data/raw/` on the host):
    1. `download_sodir.py`, `download_forge.py`, `download_volve.py` (Volve only if configured);
    2. `python -m synthetic_assam`: 30 synthetic wells, their PDFs, voice notes and the gold set;
    3. `synthetic/generate.py`: a real-time CSV for replay (below);
    4. `register.py`: uploads every document listed in `data/raw/*/register.json` to the S3 object store
       (bucket `documents` or `audio`, key = path under `data/raw`, sha256 in object metadata)
       and inserts a `document` / `audio_note` row with `source`, `synthetic`, `classification`,
       block and page count. Rows are keyed on sha256 and objects are skipped when their sha256
       matches, so reruns register nothing twice.

Each step runs on its own: a failing download does not stop the rest, but `make seed` exits
non-zero and names the failed steps. `make seed-offline` skips the three downloaders (the
handwriting fonts are still fetched once). Registered public documents use blocks `NO-SODIR`,
`US-UT-FORGE` and `NO-VOLVE-PL046` with classification `public`; synthetic ones use `SA-BLK-n`,
`internal` and `synthetic = true`. Registration runs as the table owner, like any trusted
ingestion. Facts are **not** loaded from the synthetic ground truth: extracting them from the
documents is the job being tested.

### Real-time replay CSV

`data/synthetic/generate.py` writes one deterministic well (seed 42) to `data/raw/synthetic/`:

- `well_001_survey.csv`: MD, inclination, azimuth, and TVD / TVDSS / N / E computed by
  minimum curvature (`core.depth`).
- `well_001_realtime.csv`: 2 h of 1 s drilling channels (bit depth, ROP, WOB, SPP, ECD).

Units: metres, degrees, kN, bar, sg. Depth datum: RT; TVDSS uses an RT elevation of 32 m.

## Synthetic Upper Assam wells (`data/synthetic_assam`)

**Why synthetic.** Real Indian well records are confidential, and public records (Sodir, Volve,
FORGE) come without field-level ground truth. To measure extraction, retrieval and alerting we
need documents whose every value is known. The generator produces wells in an Upper
Assam-style setting, renders their reports the way real archives look, and keeps the truth.
**Nothing synthetic may be read as geology or engineering fact.**

**Stratigraphy is a placeholder.** `data/synthetic_assam/stratigraphy.toml` lists formations
(Dhekiajuli, Girujan, Tipam, Barail, Kopili, Sylhet, Langpar by default) with thickness, rate of
penetration, mud weight and event-probability ranges. The names follow the commonly published
Upper Assam shelf succession; every number is invented. A geologist must confirm or replace it and
sign `confirmed_by`. Pass `--config` to use another file.

### How ground truth is generated

For each of 30 wells (`python -m synthetic_assam --wells 30 --seed 1`; each well has its own
random stream, so changing one does not reshuffle the others):

1. **Location and datum.** Surface lon/lat in the configured box (block by west–east band).
   Wells spudded before 2000 print coordinates in **Kalianpur 1975** with probability 0.6,
   converted from the true WGS 84 position with `core.geodesy` (EPSG:1156). Ground elevation
   88–132 m; depth reference **KB or RT** 5.5–9.5 m above ground. 30 % of wells report in
   **feet**.
2. **Formations.** Thicknesses are drawn per well with an eastward-thickening trend plus noise,
   so neighbouring wells look alike. TD is 30–90 % through the Sylhet.
3. **Trajectory.** Vertical (40 %), J-type build-and-hold (40 %, 20–45°) or S-type (20 %).
   Stations every 30 m (or 100 ft); TVD, TVDSS, N, E by minimum curvature (`core.depth`).
   Formation tops are where the path crosses each top's TVD.
4. **Casing.** 13-3/8 in surface shoe at 400–600 m TVD; 9-5/8 in shoe 20–60 m above the Barail
   top; 7 in production casing to TD. A LOT or FIT below each of the first two shoes.
5. **Events.** For each formation and each type (loss, stuck pipe, kick) one draw against the
   formation's probability `p`. A hit places the event at a relative position in the formation
   (`rel_pos_power` biases Barail losses toward its top). Loss volume, pit gain, NPT, ECD over
   mud weight come from the `[events.*]` ranges; severity follows from them. Root cause is
   recorded (50 %), inferred (20 %) or unknown (30 %).
6. **Operations timeline.** Drilling in stretches of up to 8 h at the formation's ROP, stopping
   at each event for its NPT; at each shoe: circulate, trip, run casing, cement, WOC, nipple up
   BOP, trip in, occasional rig repair, drill out and LOT/FIT. At TD: logging, casing, rig down.
   All durations are whole quarter hours. The timeline is cut at midnights into **daily reports
   whose rows sum to 24 h**; depth at midnight is interpolated inside drilling rows.

The whole truth for a well (every value above, in metres and sg) is written to
`data/raw/synthetic_assam/wells/<well>/truth.json`.

### Rendered documents

Per well (271 documents for the default 30 wells):

| Document | Content | Fields recorded |
|---|---|---|
| Well completion report (`*_WCR.pdf`, 4 pages) | Header (well, block, field, coordinates + datum, KB/RT elevation, ground level, spud and rig-release dates, TD MD/TVD, profile); formation tops; casing; LOT/FIT; drilling problems | Every value, e.g. `latitude`, `datum_elevation`, `tops[3].md`, `casing[1].shoe_md`, `lot_fit[0].emw`, `events[0].volume_bbl` |
| Directional survey report (`*_SURVEY.pdf`) | MD, inclination, azimuth, TVD, N/S, E/W for every station | `survey[i].md` … `survey[i].east` |
| Daily drilling reports (`*_DDR_nnn.pdf`) | Spud day, every day with an event, one random day. Header, **time-breakdown table** (from, to, hours, code, operation; NPT rows coded `NPT-LOSS`, `NPT-STUCK`, `NPT-KICK`, `NPT-RR`), total hours, NPT today, remarks | `report_date`, `depth_md`, `progress`, `mud_weight`, `time_breakdown[i].*`, `npt_hours`, `remarks[k]` |
| Handwritten field notes (`*_NOTE_En.pdf`) | One per event: date, well, event, depth, formation, volume, MW/ECD, action, NPT | One field per line |

How a document is produced depends on the well's vintage, mimicking real archives:

| Spud year | typed (born-digital PDF) | scanned | fax |
|---|---|---|---|
| before 1995 | 10 % | 50 % | 40 % |
| 1995–2007 | 40 % | 60 % | – |
| 2008 on | 80 % | 20 % | – |

- **Typed:** reportlab, text layer intact.
- **Scanned:** rendered at 150 dpi with pdfium, then rotated ±2°, blurred, speckled, grey paper,
  and on most first pages a "RECEIVED" stamp over the text. Image-only PDF.
- **Fax:** as scanned, plus halved resolution, binarised and horizontal streaks. Decimal points
  and thin strokes disappear, as on real faxes.
- **Handwritten:** Pillow with a handwriting font (SIL OFL fonts from Google Fonts, pinned to
  one commit and checksummed; `SYNTH_HANDWRITING_FONTS` overrides), ruled paper, jittered lines,
  page rotation ±3°, noise, and fax degradation for some pre-1995 wells.

Formatting varies per well the way it does between operators and decades: depths in m (one
decimal) or ft (integer); dates as `14.03.1996`, `14-Mar-1996`, `1996-03-14` or `14/03/1996`;
coordinates in decimal degrees (WGS 84, post-2005 wells) or DMS; KB or RT.

### SYNTHETIC marking

- **Every page** has a large diagonal SYNTHETIC watermark and the footer "SYNTHETIC DATA -
  generated by synthetic_assam for software testing. Not a real well." Faxed pages, where grey
  would be binarised away, get an outlined watermark and a bold footer instead.
- **Every PDF** has subject `SYNTHETIC DATA - not a real well` and keywords starting
  `SYNTHETIC; synthetic_assam; seed=…`. Operator is "Synthetic Operator Ltd.", fields are
  "Field Alpha" … "Field Echo", blocks `SA-BLK-n`: none can be mistaken for a real one.
- **Every JSON** file (`truth.json`, `documents.json`, `gold_set.json`) has `"synthetic": true`.
- In Postgres, registered documents and audio notes carry `synthetic = true` (see below).

Rebuilt PDFs are byte-identical (fixed dates and ids, seeded noise), so checksums are stable.

### Voice notes

Up to two per well with events (about 45 for the default wells): short "expert" notes spoken by
**espeak-ng**, fully offline, in `wells/<well>/audio/<event>.wav` with the exact transcript in
`<event>.txt`.

- Each note **starts by saying "This is a synthetic recording."** (an audible watermark), then
  gives well, depth in the well's reporting unit, formation, what happened, volume, mud weight,
  actions, recorded cause and lost hours. The transcript is exactly the text spoken, so it is
  ground truth for speech-to-text and for fact extraction from audio.
- Accents vary by espeak-ng voice: American, British RP, Scottish, Caribbean, Lancashire,
  West Midlands; speed 140–175 wpm and pitch vary too. **espeak-ng has no Indian English voice**:
  add a licence-checked model (for example a Piper voice) before claiming coverage of Indian
  speakers. Voices are robotic; treat this as a pipeline test, not an accuracy benchmark for
  real field recordings.
- Each WAV has a RIFF `LIST/INFO` chunk: `ICMT` "SYNTHETIC DATA - generated by synthetic_assam
  with espeak-ng. Not real.", `INAM` title, `IART` speaker role and accent, `ISFT` voice.
- espeak-ng is GPL-3.0 and is only run as a separate program; its audio output is not covered by
  the GPL. It is installed in the `datatools` container. Without it, voice notes are skipped
  with a warning.

### Synthetic truth as accepted facts, and the assistant evaluation set

`python -m synthetic_assam.load_truth` (from `data/`) writes the ground truth straight into
Postgres, as if the extraction pipeline had read every Well Completion Report perfectly:
wells, wellbores, formation tops, casing, LOT/FIT, drilling problems with mitigations, each
citing the WCR page and bbox where it is printed (same layout as the rendered PDFs), plus the
field-note narratives and voice-note sentences (with time spans) as retrieval passages. Facts
are `accepted` by reviewer `synthetic-truth`; documents and voice notes are `synthetic`. Used
for demos without OCR and for the assistant evaluation.

`python -m synthetic_assam.assistant_eval` writes `apps/api/tests/assistant_eval.jsonl`: 100
questions with expected values and citations from the same truth (see
[Assistant → Evaluation](assistant.md#evaluation)).


Besides the WCR facts, the loader writes what the canonical and intelligence steps would add:

- **Datum confidence and position uncertainty** per well: the WCR states the coordinate datum, so
  it is `known`; the radius is the transformation accuracy (`core.canonical.crs.to_wgs84`): 0 m
  for WGS 84, 22 m for Kalianpur 1975 (9 of 30 wells).
- **Fault block** per well: a *synthetic* structural interpretation (each field cut by one NE–SW
  fault through its centroid, parallel to the Naga thrust front; the block is the field plus the
  side, e.g. `Alpha-NW`). A placeholder for a geologist's interpretation, so the analogue
  fault-block factor and the offset map have data.
- **Daily mud weights** (`mud_record`, depth and sg) from the DDRs the generator renders for every
  event day and day 1, each cited to the DDR page and box.

### Gold set

`data/raw/synthetic_assam/gold/gold_set.json`: **50 documents** with field-level ground truth,
chosen deterministically (seed) and stratified so that every way a document can look is tested:

| Document | typed | scanned | fax | handwritten |
|---|---|---|---|---|
| WCR | 5 | 5 | 3 | |
| DDR | 7 | 8 | 5 | |
| Survey report | 5 (any) | | | |
| Field note | | | | 12 |

If a stratum has too few documents (small runs), the set is filled from the rest.

Each document entry: `id`/`path` (under `data/raw`), `doc_type`, `render`, `well`, `pages`,
`depth_unit`, `sha256`, `vintage`, and `fields`, each with:

| Key | Meaning |
|---|---|
| `name` | Field name; list items indexed: `tops[2].md`, `time_breakdown[0].hours`, `events[1].type` |
| `printed` | Exact string typed or written on the page (before scan damage) |
| `value` | Canonical value of what is printed: metres (a depth printed in feet converted from the printed number, so exact match is possible), sg, ISO dates, enum names. Coordinates in the datum named by `coordinate_datum`; the WGS 84 truth is in `truth.json` |
| `unit` | Unit as printed (`ft`, `m`, `sg`, `bbl`, `h`, `in`, `deg`) |
| `page`, `bbox` | 1-based page; `[x0, y0, x1, y1]` as fractions of the page, origin top-left, **after** scan rotation |

The default set holds about 5,200 fields. `documents.json` has the same field truth for all
271 documents, and `truth.json` per well has everything, including values never printed (true
WGS 84 position, exact depths) for evaluating datum and unit handling.

Evaluating an extractor: match on `name`, compare `value` (numbers with a tolerance matching the
printed precision: 0.1 m, 1 ft, 0.01 sg), and score citations by IoU of the predicted region
with `bbox`.

### Tests

`data/tests` (run with `make test`): every daily report sums to 24 h and holds every event;
casing and tops are ordered; same seed gives the same well; a rotated box's pixels match
`rotate_bbox`; every recorded `printed` string is in its page's text layer; every page and PDF is
marked SYNTHETIC; rebuilt documents are byte-identical; the gold set is stratified and
deterministic; a voice note (with a stubbed TTS) keeps a valid WAV with the INFO chunk and
transcript. Downloader tests use a local HTTP server: resume with `Range`, no second download,
checksum mismatch rejected, a changed pinned file rejected.

## Open items

| Item | Owner | Why it matters |
|---|---|---|
| Confirm or replace `stratigraphy.toml` (names, thicknesses, probabilities); fill `confirmed_by` | Geologist | Until then every synthetic depth and event rate is invented |
| Accept the Volve licence; set `VOLVE_SAS_URL` or `VOLVE_DIR` | Whoever holds the licence | Volve is skipped without it |
| Commit `data/checksums.lock.json` after the first successful `make seed` | Developer | Pins every static download; later changes are rejected |
| Add an Indian English TTS voice (licence-checked) | Developer | espeak-ng accents do not cover Indian speakers |
| Check Sodir terms before redistributing any downloaded wellbore PDF | Project lead | FactPages data is NLOD; licensee reports may carry other terms |
