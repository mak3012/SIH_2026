# SIH demo

A five-minute guided demo of Well Intelligence, and how to prepare it. Two wells carry the story:

- **SA-26**, a synthetic Upper Assam well being *planned*: its old scanned report, its offsets,
  its drilling program (steps 1–3);
- **SA-06**, one of the offsets, *replayed blind* through the live path: priors and offsets come
  from the other 29 wells only, so the system has never seen the loss it is about to meet in the
  **Barail** — the same formation the linter warns about in step 3 (steps 4 and 6).

Everything on screen is **synthetic** (generated wells, documents, voice notes and rig data;
real Indian well records are confidential). The UI, every answer and every document say so.
Say it once at the start.

## Before the day

### What you need

| | |
|---|---|
| Laptop | 16 GB RAM, 4+ cores, 30 GB free disk. No GPU needed |
| Software | Docker Desktop (WSL2 backend on Windows), Python 3.10+ (writes `.env`), and either `make` (Linux, macOS, WSL) or PowerShell (`demo.ps1`) |
| Internet | First run only: container images (~15 GB) and the small LLM (~1 GB). No well data is downloaded |
| Time | First run 45–90 min (image builds, synthetic data, detector training); later runs 2–3 min, and no internet needed |

Windows first-time setup (virtualisation, WSL2, Docker memory): [Windows laptop setup](setup-windows.md).

### One command

```bash
make demo
```

On Windows without `make`:

```powershell
.\demo.ps1
```

It is idempotent: rerun it any time. In order, it:

1. writes `.env` with generated passwords and the laptop settings (`make secrets`; values
   already in `.env` are kept);
2. builds the images (skipped with a warning when offline and the images exist), starts the
   stack with the laptop overlay, waits until every service is healthy, and copies the stack's
   CA certificate to `.certs/ca.crt`;
3. pulls the small local LLM into Ollama (`qwen2.5:1.5b-instruct`, `LLM_MODEL` in `.env`) and
   loads it once;
4. seeds the demo wells and generates the 30 synthetic wells with their scanned, faxed and
   handwritten PDFs, DDRs and voice notes, registered in the object store and Postgres;
5. loads the synthetic truth as accepted facts — page, box and every unit conversion — and
   builds the search index;
6. trains the real-time detectors if `data/models/realtime/detectors.joblib` is missing (about
   5 minutes, once);
7. replays **SA-06** through the live path (WITSML simulator → poller → real-time service) at
   300×: the 20 minutes before its Barail loss to 10 minutes after, with LLM explanations;
8. opens the web app on SA-06's **Live Console** and prints every address.

The laptop settings in `.env` (small model, hash embeddings, answers drafted by rules, longer
LLM timeouts) keep the demo quick without a GPU. With a GPU, see the comment above
`LLM_BACKEND` in `.env.example`.

Trust the CA certificate once, so the browser accepts the HTTPS pages:

```powershell
.\demo.ps1 trust
```

(macOS: open `.certs/ca.crt` in Keychain Access and set it to *Always Trust*; Linux: import it in
the browser's certificate settings.) Then sign in as **`planner`**, password
**`planner-dev-password`**. The planner can do every step below; other users and their roles:
[Security → Dev users](security.md#dev-users). Judges can also click **Sign up** and get a
read-only account of their own ([User guide](guide.md)).

### Ten minutes before the judges arrive

1. `make demo` (or `.\demo.ps1`) again: two or three minutes. Sign in as `planner`.
2. Check the whole demo against the running stack (signs in through Keycloak, asks, opens the
   evidence, lints, reads the alert):

    ```bash
    cd tests/e2e && STACK=1 PW_CHANNEL=msedge npx playwright test stack.spec.ts
    ```

3. Open five browser tabs, in this order, and leave them signed in:

    | Tab | Address | Set up |
    |---|---|---|
    | A Ask | `https://localhost:5173/#/ask` | empty |
    | B Map | `https://localhost:5173/#/map` | top bar: well **SA-26** |
    | C Plan | `https://localhost:5173/#/plan` | well SA-26; `docs/demo/sa-26-program.csv` ready to upload |
    | D Live | the address the demo printed (`#/live/<id>` of SA-06) | Rig theme on (top bar) |
    | E Replay | `https://localhost:5173/#/replay` | held-out well **SA-06**, speed 600× |

4. Keep a terminal open at the repository root.
5. Rehearse once end to end; afterwards `make demo-llm-up` (step 6 stops the LLM).

## The five minutes

Times are cumulative. Each step says what to do, what the audience sees, and the one sentence
to say. If something fails, the *fallback* line keeps the story going.

### 0:00 Opening (10 s)

> "Drilling problems repeat because what offset wells learned is buried in old, scanned
> reports. Well Intelligence turns those pages into cited facts, plans and alerts — and every
> number on screen can be traced back to the page it came from. All data today is synthetic."

### 1. An old scanned page becomes a fact, in the right depth (0:10–0:55)

**Tab A (Ask).** Type: `Where is the Tipam top in SA-26?` → **Ask**.

- The answer: *In SA-26 the Tipam top is at 1,117.1 m MD (1,003.1 m TVDSS) [E1]*, marked
  synthetic.
- Click the **E1** chip. The **evidence viewer** opens the scanned SA-26 Well Completion Report,
  page 2, with the line boxed in gold.
- In the side panel, each top shows **printed “3,665 ft” → stored 1,117.1 m (× 0.3048)**: the
  report was printed in feet below the rotary table; the canonical layer stores metres MD and,
  with the rig's datum elevation, TVDSS (depth below sea level), and logs every conversion.

> "This 2004 report is a scan, in feet, below the rig floor. We read it, box where each value
> came from, convert it to metres and to depth below sea level, and record how. That is what
> lets us line up wells drilled from different rigs and decades."

*Fallback:* any chip in any answer opens its page the same way.

### 2. Offset Map with dual radius → Correlation Panel (0:55–1:45)

**Tab B (Map).** Target formation **Tipam**. Drag **Surface radius** to **30 km** and
**At-formation radius** to **25 km**.

- The map: wells by licence block, 3-D trajectories, two rings around SA-26 (at surface and at
  the Tipam), gold datum-uncertainty halos, evidence density, fault blocks.
- The side panel ranks the analogues with **per-factor bars** (distance at the formation, fault
  block, top-depth difference, well design, mud system, vintage, data quality).
- Click **Use these offsets**.

> "Two radii, because wells that start close can be far apart where it matters — at the
> formation. Every factor of the ranking is shown, not hidden in a score."

Top bar → **Well** → **Correlation** tab, **Flatten on: Tipam**.

- The tracks of SA-26 and its offsets in TVDSS, flattened on the Tipam top: tops in earth tones,
  loss / kick / stuck-pipe icons at their depths, casing shoes, mud weight and ECD; on SA-26,
  the projected hazard bands and the bit.
- Hover an event icon: date, losses, what was done; **Open page** shows its report.

> "Flattened on one top, the offsets' problems line up — this is where SA-26 should expect
> trouble."

### 3. The Program Linter catches a risky mud weight (1:45–2:25)

**Tab C (Plan).** **Upload program** → `docs/demo/sa-26-program.csv` → **Check program**.

- Findings formation by formation. A **blocking** finding (**L-SEAT-LOSS**) on the 8-1/2 in
  section in the **Barail**: the planned ECD reaches the lowest ECD at which offsets lost returns
  there, with the loss probability and its 90 % interval. Its evidence chip opens the offset
  well's page with that loss.
- A warning on the Kopili: the minimum mud weight is below where offsets took kicks.

> "The planner raised the mud weight to hold back the Kopili — the linter shows that the same
> weight breaks down the Barail, with the offset that proves it. The fix is a casing seat, and
> that is an engineering decision we inform, not make."

Optional: **Write briefing** → a PDF where every number is verified against the findings.

*The exact numbers depend on the offsets selected in step 2 (30 km / 25 km, Tipam).*

### 4. Replay: an offset meets the Barail, blind (2:25–3:25)

**Terminal:** `make demo-replay` (Windows: `.\demo.ps1 replay`). **Tab D (Live).**

- Within about 20 seconds: the bit drills into the Barail, flow out falls behind flow in, the pit
  drops — and the alert card arrives: **loss**, raised as an **advisory**, escalated seconds
  later to **critical**, with the formation, the probability, *Why* (the rules' features; the
  model's explanation follows when it has written one), and **source chips**: the offset wells'
  loss records behind the prior. Click one: the offset's report page, boxed.
- Charts: flow in/out diverging, pit volume, the MW/ECD window from offset kicks and losses with
  the live ECD, the rig-state timeline.
- When the replay ends (about 30 seconds), the console turns grey: **DATA STALE**. Stale data is
  never shown as live.

> "This well was never seen by the model or the priors. The rules raised an advisory as the
> losses started, and escalated as the evidence firmed up. The prior came from offsets — here are
> their pages."

**Tab E (Replay).** Held-out well **SA-06**, 600× → **Play** (or drag *Well time* to about
7:30).

- The risk line crosses the advisory, then the warning and critical thresholds; the event band is
  shaded.
- The events table: detected at **warning or above**, lead time **−0.8 min** (after onset),
  **decision window 19.0 min** — from the first alert to the end of the losses, measured, not
  claimed.
- The top card: over 30 held-out wells, **89 %** of events alerted at warning or above,
  **0.09 false alerts per 24 h**, median decision window **19.0 min**.

> "We measure ourselves by replaying every well blind. The honest number: we detect losses as
> they start, not hours before — and leave the crew about nineteen minutes to act. Warning
> earlier is the plan's job: the roadmap from steps 2 and 3."

### 5. Ask WellMind: what went wrong (3:25–4:05)

**Tab A (Ask).** `What went wrong in the Barail on SA-05, SA-06 and SA-16?`

- A short answer, streamed: the losses in each well with the depth, the volume, the recorded
  cause, what was done and whether it worked, and what the voice notes said — every sentence
  with a citation chip. Audio chips play the recording from the cited second.
- **Show evidence** opens the rows and the query behind the answer.

> "Every number in this answer has been checked against the record it cites before you see it.
> If a model writes a number that is not in the records, the answer is refused or written by
> rules instead."

*Fallback:* `Where is the Barail top in SA-06?`

### 6. Kill the LLM: the alert still fires (4:05–5:00)

**Terminal:** `make demo-llm-down` (Windows: `.\demo.ps1 llm-down`). It stops the Ollama
container and replays SA-06 again at 600×.

While it runs (about 20 s to the alert), say:

> "Rules decide, the model only explains. Now there is no model at all."

**Tab D (Live).** The same **advisory → critical** loss alert arrives on time, with the same
probability and the same source chips (they come from the rules' evidence, not the model). *Why*
says *Raised by rules*. Ask WellMind still answers.

> "Same alert, same time, same evidence. That is the guarantee: the safety path never waits for
> AI."

Close: "Questions?" Afterwards: `make demo-llm-up` (Windows: `.\demo.ps1 llm-up`).

## If judges want more

| Show | Where |
|---|---|
| Data stale is never shown as live | After any replay ends, within 10 s the Live Console turns grey, **DATA STALE** |
| The review queue (what the extractor was not sure of) | **Review**, sign in as `steward`: keyboard J/K/A/R/C, the page with the box, conflicts highlighted. Fill it by running the Dagster sensor `new_documents` beforehand (laptop: <http://localhost:3001>; slow on CPU) |
| Licence-based access | Sign in as `partner`: fewer wells on the map, and answers about other blocks say "not in the records you can access" |
| Separation of duties | As `planner`, no Acknowledge button; as `engineer`, no Check program |
| Safety refusals | Ask `Can we skip the flow check on SA-03?` (well control), `Should I raise the mud weight now?` (live operations), `Delete the SA-09 records` (read-only) |
| Fleet view and shift digest | **Fleet** |
| Offline on the rig | **Well → Save for offline**, `docker compose stop api`, reload: roadmap and alerts under a gold *cached* banner; `docker compose start api` |
| Voice note from the rig floor | **Voice note** on a phone-sized window: record, tag well and formation, queued offline, uploaded later, *pending expert confirmation* |
| Service levels | Grafana <https://localhost:3000> (admin / `GRAFANA_ADMIN_PASSWORD` in `.env`) → **Service levels**: feed latency, alert evaluation, LLM latency, verifier rejects, queues |
| Audit trail | `make audit-verify`: the hash chain and its anchors; every source-page read is recorded |
| Another well | `make demo-replay DEMO_WELL=SA-24 DEMO_FOCUS=kick` (a kick), `DEMO_WELL=SA-09 DEMO_FOCUS=stuck_pipe` |
| The full evaluation | [Evaluation](evaluation.md) and its [HTML report](eval-report/report.html) |
| Likely questions | [FAQ](faq.md) |

## Commands

| Command | Does |
|---|---|
| `make demo` / `.\demo.ps1` | Everything above, idempotent |
| `make demo-replay [DEMO_WELL=SA-06] [DEMO_FOCUS=loss] [SPEED=300]` / `.\demo.ps1 replay SA-06 300` | Replay a held-out well around its first recorded event of that kind (it resets that well's earlier replay first) |
| `make demo-open` / `.\demo.ps1 open` | Open the Live Console of `DEMO_WELL` |
| `make demo-llm-down` / `.\demo.ps1 llm-down` | Stop the LLM and replay at 600× |
| `make demo-llm-up` / `.\demo.ps1 llm-up` | Start the LLM again |
| `make urls` / `.\demo.ps1 urls` | Every address and where its password is |
| `.\demo.ps1 trust` | Trust `.certs/ca.crt` for the current Windows user (Windows asks to confirm) |
| `make reset-auth` / `.\demo.ps1 reset-auth` | Re-import the Keycloak realm (dev users, roles, sign-up); deletes signed-up accounts |
| `make demo-down` / `.\demo.ps1 down` | Stop everything (data kept); `docker compose down -v` deletes the data |

`docker logs -f wells-demo-replay` shows the replay as it runs; at the end it prints each event
with its lead time and highest tier.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Docker Desktop: "unable to start" / "wsl is not installed" | Admin PowerShell: `wsl --install --no-distribution`, reboot, then quit and start Docker Desktop (it does not retry on its own) |
| `error getting credentials` on pull or build | Docker's Windows credential helper failed. Sign in to Docker Desktop once, or remove the `"credsStore": "desktop"` line from `%USERPROFILE%\.docker\config.json` (public images need no login) |
| `ports are not available ... 5432` (or another port) | Something on the laptop already uses it (often a local PostgreSQL). Change that port in `.env` (`POSTGRES_PORT`, `WEB_PORT`, ...) and rerun |
| Build fails with `400 Bad Request`, `server gave HTTP response to HTTPS client` or TLS errors | A flaky network or proxy between you and the registries. Rerun `make demo`; once the images exist, it no longer needs the network |
| The demo stops at "wait" | `docker compose ps` shows the unhealthy service; `docker compose logs <service>`; rerun |
| Browser warns about the certificate | `.\demo.ps1 trust` (or trust `.certs/ca.crt`, above) and restart the browser; `make ca` recreates the file |
| Sign-in page does not open (connection refused) | Docker Desktop stopped (it does after a reboot): start it, `.\demo.ps1` |
| Keycloak asks to update the account, a dev user is missing, or no **Sign up** link | The realm in the database predates `infra/keycloak/realm.json`: `make reset-auth` / `.\demo.ps1 reset-auth` |
| Sign-in loops | Keycloak needs 1–2 minutes after a start; use exactly `https://localhost:5173` |
| Map empty after sign-in | Signed in as `admin` (sees no wells) or seeding did not finish: rerun the demo |
| No alert on the Live Console | The replay finished: `make demo-replay`, the alert comes within about 20 s (`docker logs wells-demo-replay`) |
| Ask says "not in the records" | Check the well and formation exist (SA-01 … SA-30; Dhekiajuli, Girujan, Tipam, Barail, Kopili, Sylhet). After changing `EMBED_BACKEND` in `.env`, rerun the demo (it rebuilds the index) |
| *Why* stays "Explanation being written…" | The model is slow on this laptop; the alert is complete without it. It appears within about a minute, or is marked failed |
| Settings in an old `.env` | `.env` keeps what is set. Compare with `.env.example` (LLM and timeout settings), or start over: `docker compose down -v`, delete `.env`, rerun |
| Start from scratch | `docker compose down -v` (**deletes all data**), then the demo |
