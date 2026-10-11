import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Page, Route } from "@playwright/test";

// The API, mocked from tests/e2e/fixtures: responses captured from the real API over the
// synthetic dataset (fixtures/capture.py). Two things are adjusted here, and only these:
// - the replay ran on a synthetic clock in 2030, so timestamps are shifted to end "now";
// - the active well's feed is live, streamed sample by sample from its captured history.

export const API = "http://localhost:8000";
const DIR = join(__dirname, "fixtures");
export const fx = <T = any>(name: string): T => JSON.parse(readFileSync(join(DIR, `${name}.json`), "utf-8")) as T;
export const ACTIVE = 19; // SA-19

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

/** Shift every ISO timestamp in `data` by the same amount, so that `latest` maps to now. */
export function toNow<T>(data: T, latest: string, now = Date.now()): T {
  const dt = now - Date.parse(latest);
  const walk = (v: unknown): unknown => {
    if (typeof v === "string" && ISO.test(v) && v.startsWith("2030")) return new Date(Date.parse(v) + dt).toISOString();
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x)]));
    return v;
  };
  return walk(data) as T;
}

export function fakeToken(roles = ["wellsite_engineer", "planner", "data_steward", "admin"], name = "a.engineer"): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none" })}.${b64({ preferred_username: name, realm_access: { roles }, exp: 4_000_000_000 })}.sig`;
}

export async function signedIn(page: Page, roles?: string[]) {
  const t = fakeToken(roles);
  await page.addInitScript((tok) => {
    sessionStorage.setItem("oidc", JSON.stringify({ access_token: tok, expires_at: Date.now() + 3_600_000 }));
    sessionStorage.setItem("access_token", tok);
    localStorage.setItem("wi-selected", "19");
  }, t);
}

// Two public (Sodir) wells: one long known, one new.
const PUBLIC_WELLS = [
  { id: 8228, name: "7435/12-1", source: "sodir", lat: 74.07, lon: 35.81, area: "BARENTS SEA", purpose: "WILDCAT", status: "P&A", content: "GAS", operator: "Lundin Norway AS", field: null, entry_date: "2017-08-09", completion_date: "2017-09-01", total_depth_m: 1540, water_depth_m: 253, formation_at_td: "KOBBE FM", factpage_url: "https://factpages.sodir.no/wellbore/pageview/exploration/all/8228", press_release_url: null, first_seen: "2026-10-01T05:00:00Z", is_new: false, reports: 1 },
  { id: 9999, name: "25/2-23 S", source: "sodir", lat: 59.75, lon: 2.4, area: "NORTH SEA", purpose: "WILDCAT", status: "DRILLING", content: null, operator: "Equinor Energy AS", field: null, entry_date: "2026-10-04", completion_date: null, total_depth_m: null, water_depth_m: 110, formation_at_td: null, factpage_url: null, press_release_url: null, first_seen: "2026-10-08T05:00:00Z", is_new: true, reports: 0 },
];

type Handler = (route: Route, m: RegExpMatchArray) => Promise<void> | void;
const url = (r: Route) => r.request().url().slice(API.length);

export type MockOptions = {
  /** Override or fail single endpoints: path regex source -> handler. */
  override?: Record<string, Handler>;
  /** Stream the active well live (default) or leave the feed silent. */
  live?: boolean;
  /** Milliseconds between streamed samples. */
  sampleMs?: number;
};

export async function mockApi(page: Page, opts: MockOptions = {}) {
  const history = fx("history");
  const last = history.samples.at(-1).ts as string;
  const now = Date.now();
  const json = (data: unknown) => (r: Route) => r.fulfill({ json: data });
  const shifted = <T>(name: string) => toNow(fx<T>(name), last, now);
  const wells = fx<any[]>("wellbores").map((w) => (w.wellbore_id === ACTIVE ? { ...w, feed_state: "live" } : w));
  const fleet = shifted<any>("fleet");
  fleet.rigs = fleet.rigs.map((r: any) => (r.wellbore_id === ACTIVE ? { ...r, feed_state: "live", age_s: 2 } : { ...r, age_s: Math.round((now - Date.parse(r.last_sample_ts)) / 1000) }));
  const routes: [RegExp, Handler][] = [
    [/\/wellbores$/, json(wells)],
    [/\/wellbores\/\d+\/formations$/, json(fx("formations"))],
    [/\/wellbores\/\d+\/scene$/, json(fx("scene"))],
    [/\/active\/\d+\/hazard-profile$/, json(fx("hazard-profile"))],
    [/\/active\/\d+\/lookahead$/, json(shifted("lookahead"))],
    [/\/active\/\d+\/alerts$/, json(shifted("alerts"))],
    [/\/active\/\d+\/history(\?.*)?$/, json(shifted("history"))],
    [/\/active\/\d+\/envelope$/, json(fx("envelope"))],
    [/\/offsets\/preview\/\d+(\?.*)?$/, json(fx("offsets-preview"))],
    [/\/offsets\/select$/, json(fx("offsets-select"))],
    [/\/public-wells$/, json(PUBLIC_WELLS)],
    [/\/public-wells\/status$/, json({ wells: PUBLIC_WELLS.length, last_sync: new Date(now).toISOString(), newest_spud: "2026-10-04", new_days: 30 })],
    [/\/correlation\/\d+(\?.*)?$/, json(fx("correlation"))],
    [/\/fleet(\?.*)?$/, json(fleet)],
    [/\/admin\/status$/, json(fx("admin-status"))],
    [/\/review\/items(\?.*)?$/, json(fx("review"))],
    [/\/review\/items\/\d+$/, (r) => r.fulfill({ json: { id: 1, status: "accepted", resolved_by: "a.engineer" } })],
    [/\/documents\/\d+\/fields$/, json(fx("document-fields"))],
    [/\/documents\/\d+\/file$/, (r) => r.fulfill({ body: readFileSync(join(DIR, "document.pdf")), contentType: "application/pdf" })],
    [/\/source-refs\/\d+$/, json({ document_id: 63, document: "SA-19 WCR (SYNTHETIC)", page: 1, bbox: [0.1, 0.2, 0.5, 0.24], audio_note_id: null, t_start: null, t_end: null, review_status: "accepted", synthetic: true })],
    [/\/programs\/lint$/, json(fx("lint"))],
    [/\/briefings\/\d+$/, json(fx("briefing"))],
    [/\/briefings\/\d+\/\d+\.pdf$/, (r) => r.fulfill({ body: readFileSync(join(DIR, "briefing.pdf")), contentType: "application/pdf" })],
    [/\/alerts\/\d+\/ack$/, json({ ok: true })],
    [/\/active\/\d+\/shelves$/, json({ id: 1 })],
    [/\/voice-notes$/, (r) => (r.request().method() === "POST" ? r.fulfill({ status: 201, json: { id: 901, status: "pending_confirmation", duplicate: false } }) : r.fulfill({ json: [] }))],
    [
      /\/assistant\/ask\/stream$/,
      (r) => {
        const q = r.request().postDataJSON().question as string;
        const a = fx<any[]>("ask").find((x) => x.question === q) ?? fx<any[]>("ask")[0];
        const ev = (e: string, d: unknown) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`;
        const body = [ev("status", { stage: "answering" }), ...(a.answer.match(/\S+\s*/g) ?? []).map((t: string) => ev("delta", { text: t })), ev("done", a)].join("");
        return r.fulfill({ body, contentType: "text/event-stream" });
      },
    ],
    [/\/assistant\/ask$/, (r) => r.fulfill({ json: fx<any[]>("ask")[0] })],
  ];
  await page.route(`${API}/**`, async (route) => {
    const url = route.request().url().slice(API.length);
    for (const [re, h] of routes) {
      const m = url.match(re);
      if (m) return h(route, m);
    }
    return route.fulfill({ status: 404, json: { detail: `not mocked: ${url}` } });
  });
  // Overrides are routes of their own, registered later so they run first; `route.fallback()`
  // in one hands the request to the fixtures above.
  for (const [src, h] of Object.entries(opts.override ?? {})) {
    const re = new RegExp(src);
    await page.route((url) => url.href.startsWith(API) && re.test(url.href.slice(API.length)), (route) => h(route, url(route).match(re)!));
  }
  // Screenshots must not depend on internet tiles: the offline basemap, and no tile requests.
  await page.addInitScript(() => localStorage.setItem("map.basemap", "offline"));
  await page.route(/tiles\.openfreemap\.org|server\.arcgisonline\.com/, (r) => r.abort());
  // Keycloak is never reached in these tests.
  await page.route("http://localhost:8080/**", (r) => r.fulfill({ body: "keycloak" }));

  // The posterior the live path computed for SA-19 in the replay, matched by bit depth.
  const tl = JSON.parse(readFileSync(join(__dirname, "../../docs/eval-report/timelines/SA-19.json"), "utf-8"));
  const posteriorAt = (md: number) => {
    let best = 0;
    for (let i = 0; i < tl.md.length; i++) if (tl.md[i] != null && Math.abs(tl.md[i] - md) < Math.abs((tl.md[best] ?? 1e9) - md)) best = i;
    return Object.fromEntries(Object.entries(tl.post as Record<string, number[]>).map(([h, v]) => [h, v[best]]));
  };
  const samples = (toNow(history.samples, last, now) as any[]).map((x) => ({ ...x, posterior: posteriorAt(x.values.bit_md ?? 0) }));
  await page.routeWebSocket(/\/active\/\d+\/stream$/, (ws) => {
    let timer: ReturnType<typeof setInterval> | undefined;
    ws.onMessage((m) => {
      if (JSON.parse(String(m)).type !== "auth") return;
      const alerts = shifted<any[]>("alerts");
      const tail = samples.slice(-120);
      ws.send(JSON.stringify({ type: "snapshot", wellbore_id: ACTIVE, feed: { state: opts.live === false ? "stale" : "live", connector: "witsml" }, stale_after_s: 6, sample: tail[0], alerts }));
      if (opts.live === false) return;
      let i = 0;
      timer = setInterval(() => {
        const s = tail[i++ % tail.length];
        ws.send(JSON.stringify({ type: "sample", ...s, ts: new Date().toISOString(), formation: "Girujan" }));
      }, opts.sampleMs ?? 1000);
    });
    ws.onClose(() => clearInterval(timer));
  });
}
