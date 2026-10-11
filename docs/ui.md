# UI

React 19 + Vite app in `apps/web`, served by nginx at <http://localhost:5173>. Components and
design tokens live in `packages/ui` ([design system](ui/design-system.md)); pages use hash routes
so the static server needs no rewrite rules and the PWA works offline.

## Shell

Left navigation (Map, Well, Live, Plan, Ask, Voice note, Review, Fleet, Replay, Admin; a drawer
on narrow screens) and a top bar with the well selector, the rig-state pill, the data-freshness
indicator (grey and labelled when the feed is not live, with the age of the last sample), the
user and roles, the Office / Rig theme toggle and sign in / out. A skip link jumps to the page.
Every page except Map's shell loads on demand; MapLibre and deck.gl load with the offset map,
ECharts with the first chart, PDF.js with the first evidence page, three.js with the 3D tab.

What each screen shows, how to read it and a user guide per role: **[Screens](ui/screens.md)**
(with screenshots).

| Route | Screen | API |
|---|---|---|
| `#/map` | Offset map: wells, 3D trajectories, surface and at-formation radii, datum-uncertainty rings, evidence-density heat, fault blocks; analogue ranking with per-factor bars; select offsets | `GET /offsets/preview/<id>`, `POST /offsets/select` |
| `#/well/<id>` | Hazard roadmap strip with the next hazard and ETA, the roadmap matrix and lookahead; **Save for offline** | `GET /active/<id>/hazard-profile`, `/lookahead` |
| `#/well/<id>/correlation` | Correlation panel: tracks flattened on a top, events, shoes, MW/ECD/LOT, projected bands, bit | `GET /correlation/<id>` |
| `#/well/<id>/3d`, `/alerts`, `/offsets` | 3D subsurface; alerts with acknowledge and shelve; offsets | `GET /wellbores/<id>/scene`, `/active/<id>/alerts`, `/wellbores/<id>/formations` |
| `#/live/<id>` | Live console: alert card, channel charts, hookload vs T&D envelope, MW/ECD window, rig-state timeline, probabilities; grey when stale | `WS /active/<id>/stream`, `GET /active/<id>/history`, `/envelope` |
| any evidence chip | Evidence viewer: PDF page with the value boxed and the extracted fields; audio at the cited second | `GET /documents/<id>/file`, `/fields`, `/audio-notes/<id>[/file]`, `/source-refs/<id>` |
| `#/ask` | Ask WellMind: streamed answers, citations, evidence drawer, voice, suggestions, wells as table and map | `POST /assistant/ask/stream`, `/assistant/ask/voice` |
| `#/plan/<id>` | Program linter: upload or edit, findings formation by formation with the offsets cited, briefing and PDF | `POST /programs/lint`, `/briefings/<id>` |
| `#/voice` | Voice note capture (phone): record, tag, queue offline, upload, status | `POST /voice-notes`, `GET /voice-notes` |
| `#/review` | Review console: queue by value, keyboard J/K/A/R/C, page and fields, conflicts | `GET /review/items?order=value`, `POST /review/items/<id>` |
| `#/fleet` | Fleet board: next hazard and ETA, alert load, freshness per rig; shift digest | `GET /fleet` |
| `#/replay` | Replay studio: held-out well at 60–600×, risk against thresholds, measured lead time and false alerts | none: `docs/eval-report/metrics.json`, `wells.json`, `timelines/*.json`, built in |
| `#/admin` | Record counts, detector bank, LLM configuration, feeds (admin only) | `GET /admin/status` |

Each action is offered only to the roles that hold its permission ([Security → Identity and
roles](security.md#identity-and-roles)): selecting offsets and linting to planners; acknowledging,
shelving and recording voice notes to wellsite engineers and superintendents; review decisions
to data stewards; the admin page to admins. The API enforces the same rule
(`src/auth.ts` mirrors `PERMISSIONS`). In production the app is served over HTTPS by the proxy
with a content security policy (`'self'` plus the API and Keycloak origins).

Charts are ECharts on canvas (`src/components/Chart.tsx`), styled from the design tokens and
redrawn on a theme change; strokes use each meaning's text shade so they stay visible in Rig mode.
SVG views (roadmap strip, mini map) are drawn at their measured pixel size, never scaled.

## 3D subsurface

`GET /wellbores/{id}/scene` returns the active well and its offsets (the latest selection, else
wells within 10 km, at most 12) in local metres (x east, y north, z above MSL): trajectories,
formation tops, events, hazard bands from the roadmap and the bit position. The view
(react-three-fiber) draws formation tops as surfaces triangulated across the wells (earth
tones), wells as tubes (teal = this well, petrol = offsets), events as gold spheres, hazard
bands as tubes coloured by risk tier and the bit in cyan; iso / plan / section cameras and a
vertical exaggeration slider. It renders on demand (no animation loop), and a table below lists
the same content for screen readers.

## Offline (Rig mode)

The app is a PWA: the shell, scripts, styles and fonts are precached by the service worker.
Every GET a screen makes is kept in IndexedDB; with no network it is served from there under a
gold banner with the time it was saved (the **stale** state). **Save for offline** on a well
fetches its whole hazard package (roadmap, lookahead, alerts, 3D scene) in one go. Server errors
are never masked by the cache, only network failures. Voice notes recorded offline wait in
IndexedDB and upload when the browser is back online.

## Sign-in

OpenID Connect authorization code flow with PKCE against Keycloak (realm `wells`, public
client `web`, `VITE_OIDC_ISSUER`): **Sign in** redirects to Keycloak (**Sign up** to its registration form, same flow), the code comes back to
`/`, and the app exchanges it for tokens at the token endpoint. Tokens are kept in
`sessionStorage` (gone when the tab closes) and refreshed a minute before they expire; the user
name and roles shown are read from the token, which the API verifies on every request. **Sign
out** ends the Keycloak session. In the stack the app, the API (`/api`) and Keycloak (`/auth`) share one origin behind the
proxy, so no cross-origin calls happen; the API's `WEB_ORIGINS` (CORS, bearer tokens only) is
for a dev server on another port.

## Measured numbers

The UI never types a measured number by hand. `src/measured.ts` imports
`docs/eval-report/metrics.json` and `wells.json` (written by `make evaluation`) at build time;
any component that quotes a result uses `m("<key>")`; the replay studio lazy-loads
`docs/eval-report/timelines/<well>.json` (the risk score at most 1,500 points per well, bucket
maxima so no threshold crossing is lost);, which shows "not measured" when the last
evaluation lacks the key. Rebuild the web image after `make evaluation` to update them. For
other documents (the pitch deck), `python -m api.replay fill <files>` replaces
`[measured:<key>]` placeholders from the same file.

## Live view

`#/live/<wellbore id>` uses the one WebSocket the app keeps for the selected well
(`WS /active/{id}/stream`, [Real-time](realtime.md#api)); the top bar's freshness indicator
reads the same socket. When the feed is anything but live (the server says stale, no sample has
arrived for `stale_after_s`, or the socket closed) the panel turns grey and reads **Data
stale** / **Data disconnected** within 10 s. It authenticates by sending the access token as its
first WebSocket message.

## Tests

`tests/e2e` (Playwright), against the vite dev server with the API mocked from fixtures captured
from the real API (`tests/e2e/fixtures`, see [Screens](ui/screens.md)); no stack needed.
`PW_CHANNEL=msedge` uses an installed Edge instead of a downloaded Chromium; a fake microphone is
configured for voice capture.

| Spec | Checks |
|---|---|
| `flows.spec.ts` | the main flow of each screen: offset selection, correlation and the evidence viewer, roadmap ETA countdown, live console acknowledge and shelve, audio at the cited second, streamed answer with a wells table and map, CSV upload → findings by formation → briefing PDF (and a refused CSV), review keyboard J/K/A, fleet board and digest, replay playback and results, voice note recorded offline then uploaded |
| `states.spec.ts` | loading, error with retry, empty, and stale (offline cache) on the offset map, correlation, review, fleet and voice screens; live stale and no feed; failed answer; roadmap without offsets |
| `ui.spec.ts` | PKCE sign-in redirect, replay numbers from `metrics.json`, assistant citations / evidence / follow-ups, offsets → roadmap |
| `live-stale.spec.ts` | the live view greys within 10 s of the last sample |
| `smoke.spec.ts` | shell, Rig theme (keyboard, 48 px targets, persistence), API health (needs the stack) |
| `geo.spec.ts` | the map's radius circles and outlines (geodesy helpers) |
| `screens.spec.ts` | writes the screenshots in `docs/ui/screenshots` |

Lighthouse on the built shell (`vite preview`, signed out): see
[design system](ui/design-system.md#acceptance).
