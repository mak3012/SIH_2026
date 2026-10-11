import { expect, test, type Page, type Route } from "@playwright/test";
import { mockApi, signedIn } from "./mock";

// Every data screen shows four states besides "ready": loading, error (with retry), empty and
// stale (served from this device's cache when the network is gone).
test.describe.configure({ timeout: 60_000 });

type Screen = { name: string; hash: string; endpoint: string; empty: unknown; emptyText: RegExp; ready: (p: Page) => Promise<void> };

const SCREENS: Screen[] = [
  {
    name: "offset map",
    hash: "#/map",
    endpoint: "/offsets/preview/\\d+",
    empty: { wellbore_id: 19, formation: "Barail", surface_radius_m: 1, subsurface_radius_m: 1, weights: {}, wells: [] },
    emptyText: /No wells to map/,
    ready: async (p) => expect(p.getByRole("list", { name: "Analogue wells by score" })).toBeVisible(),
  },
  {
    name: "correlation",
    hash: "#/well/19/correlation",
    endpoint: "/correlation/\\d+",
    empty: { wellbore_id: 19, formations: [], datum: null, datum_choices: [], tracks: [{ well: "SA-19", tops: [], events: [], casing: [], mud: [], ecd: [], lot: [] }] },
    emptyText: /No offset wells to correlate/,
    ready: async (p) => expect(p.getByTestId("correlation-chart")).toBeVisible(),
  },
  {
    name: "review console",
    hash: "#/review",
    endpoint: "/review/items",
    empty: [],
    emptyText: /Nothing waiting for review/,
    ready: async (p) => expect(p.getByRole("list", { name: /Review queue/ })).toBeVisible(),
  },
  {
    name: "fleet board",
    hash: "#/fleet",
    endpoint: "/fleet",
    empty: { as_of: new Date().toISOString(), rigs: [], digest: { hours: 12, raised: 0, acknowledged: 0, cleared: 0, shelved: 0, by_tier: {}, items: [] } },
    emptyText: /No rigs to show/,
    ready: async (p) => expect(p.getByRole("list", { name: "Rigs, most urgent first" })).toBeVisible(),
  },
  {
    name: "voice notes",
    hash: "#/voice",
    endpoint: "/voice-notes",
    empty: [],
    emptyText: /No uploaded notes yet/,
    ready: async (p) => expect(p.getByText(/No uploaded notes yet|Pending expert/)).toBeVisible(),
  },
];

async function start(page: Page, hash: string, endpoint: string, handler: (r: Route) => unknown) {
  await signedIn(page);
  await mockApi(page, { override: { [`${endpoint}(\\?.*)?$`]: handler as never } });
  await page.goto(`/${hash}`);
}

for (const s of SCREENS) {
  test.describe(s.name, () => {
    test("loading shows a skeleton", async ({ page }) => {
      let release!: () => void;
      const gate = new Promise<void>((r) => (release = r));
      await start(page, s.hash, s.endpoint, async (r) => {
        await gate;
        return r.fallback();
      });
      await expect(page.locator("main [data-loading]").first()).toBeVisible();
      release();
      await s.ready(page);
    });

    test("error says why and retries", async ({ page }) => {
      let fail = true;
      await start(page, s.hash, s.endpoint, (r) => (fail ? r.fulfill({ status: 503, json: { detail: "database unavailable" } }) : r.fallback()));
      await expect(page.getByRole("alert")).toContainText("database unavailable");
      fail = false;
      await page.getByRole("button", { name: "Retry" }).click();
      await s.ready(page);
    });

    test("empty says so", async ({ page }) => {
      await start(page, s.hash, s.endpoint, (r) => r.fulfill({ json: s.empty }));
      await expect(page.getByText(s.emptyText)).toBeVisible();
    });

    test("stale: offline, the last data from this device, marked", async ({ page }) => {
      let offline = false;
      await start(page, s.hash, s.endpoint, (r) => (offline ? r.abort("internetdisconnected") : r.fallback()));
      await s.ready(page);
      offline = true;
      await page.reload();
      await expect(page.getByText("Offline: showing data saved on this device")).toBeVisible();
      await s.ready(page);
    });
  });
}

test("live console: stale feed is grey and labelled", async ({ page }) => {
  await signedIn(page);
  await mockApi(page, { live: false });
  await page.goto("/#/live/19");
  await expect(page.getByTestId("live-well")).toHaveAttribute("data-feed-state", "stale");
  await expect(page.getByText("DATA STALE", { exact: true })).toBeVisible();
});

test("live console: no feed at all is an empty state", async ({ page }) => {
  await signedIn(page);
  await mockApi(page);
  await page.routeWebSocket(/\/active\/\d+\/stream$/, (ws) => {
    ws.onMessage(() => ws.send(JSON.stringify({ type: "snapshot", wellbore_id: 19, feed: { state: "none", connector: null }, stale_after_s: 6, sample: null, alerts: [] })));
  });
  await page.goto("/#/live/19");
  await expect(page.getByText("No real-time feed for this well")).toBeVisible();
});

test("ask: a failed answer says why", async ({ page }) => {
  await signedIn(page);
  await mockApi(page, { override: { "/assistant/ask/stream$": (r) => r.fulfill({ status: 503, json: { detail: "assistant unavailable" } }) } });
  await page.goto("/#/ask");
  await page.getByRole("textbox", { name: "Question" }).fill("anything");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
});

test("well roadmap: no offsets yet is explained, not an error", async ({ page }) => {
  await signedIn(page);
  await mockApi(page, { override: { "/active/\\d+/hazard-profile$": (r) => r.fulfill({ status: 409, json: { detail: "no offset selection" } }) } });
  await page.goto("/#/well/19");
  await expect(page.getByText("No offset wells selected yet")).toBeVisible();
});

