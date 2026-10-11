import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { mockApi, signedIn, type MockOptions } from "./mock";

// Screenshots for docs/ui/screens.md, against the captured fixtures (mock.ts):
//   PW_CHANNEL=msedge npx playwright test screens.spec.ts     -> docs/ui/screenshots/*.png
const OUT = join(__dirname, "../../docs/ui/screenshots");
mkdirSync(OUT, { recursive: true });

test.use({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
test.describe.configure({ mode: "serial", timeout: 120_000 });

async function shot(page: Page, name: string, fullPage = false) {
  await page.waitForTimeout(600); // fonts and canvases settle (no animation: reduced motion)
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage });
}

async function open(page: Page, hash: string, theme: "office" | "rig" = "office", opts: MockOptions = {}) {
  await signedIn(page);
  await page.addInitScript((t) => localStorage.setItem("wi-theme", t), theme);
  await mockApi(page, opts);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/${hash}`);
}

test("01 offset map", async ({ page }) => {
  await open(page, "#/map");
  await expect(page.getByTestId("offset-map").locator("canvas").first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("list", { name: "Analogue wells by score" }).getByRole("button").first().click();
  await page.waitForTimeout(1500);
  await shot(page, "01-offset-map");
});

test("02 correlation", async ({ page }) => {
  await open(page, "#/well/19/correlation");
  await expect(page.getByTestId("correlation-chart").locator("canvas")).toBeVisible();
  await shot(page, "02-correlation", true);
});

test("03 hazard roadmap", async ({ page }) => {
  await open(page, "#/well/19");
  await expect(page.getByTestId("next-hazard")).toBeVisible();
  await shot(page, "03-hazard-roadmap");
});

test("04 live console, rig mode", async ({ page }) => {
  await open(page, "#/live/19", "rig");
  await expect(page.getByTestId("alert-card")).toBeVisible();
  await page.waitForTimeout(3000);
  await shot(page, "04-live-console-rig", true);
});

test("04b live console, stale feed", async ({ page }) => {
  await open(page, "#/live/19", "rig", { live: false });
  await expect(page.getByTestId("live-well")).toHaveAttribute("data-feed-state", "stale");
  await shot(page, "04b-live-console-stale");
});

test("05 evidence viewer", async ({ page }) => {
  await open(page, "#/well/19/correlation");
  await page.getByText("Events as a table").click();
  await page.getByRole("table", { name: "Offset events in the correlation" }).getByRole("button").first().click();
  await expect(page.getByTestId("pdf-page").locator("canvas")).toBeVisible();
  await page.waitForTimeout(1500);
  await shot(page, "05-evidence-viewer");
});

test("06 ask WellMind", async ({ page }) => {
  await open(page, "#/ask");
  await page.getByRole("button", { name: "Which wells had losses in the Barail?" }).click();
  await expect(page.getByTestId("wells-result")).toBeVisible();
  await shot(page, "06-ask-wellmind", true);
});

test("07 program linter", async ({ page }) => {
  await open(page, "#/plan/19");
  // The program the captured lint was run on, uploaded as a file.
  await page.getByTestId("program-upload").setInputFiles(join(__dirname, "fixtures", "program.json"));
  await page.getByRole("button", { name: "Check program" }).click();
  await expect(page.getByTestId("lint-by-formation")).toBeVisible();
  await page.getByRole("button", { name: "Write briefing" }).click();
  await expect(page.getByTestId("briefing")).toBeVisible();
  await shot(page, "07-program-linter", true);
});

test("08 review console", async ({ page }) => {
  await open(page, "#/review");
  await expect(page.getByTestId("pdf-page").locator("canvas")).toBeVisible();
  await page.waitForTimeout(1500);
  await shot(page, "08-review-console");
});

test("09 fleet board", async ({ page }) => {
  await open(page, "#/fleet");
  await expect(page.getByTestId("digest")).toBeVisible();
  await shot(page, "09-fleet-board", true);
});

test("10 replay studio", async ({ page }) => {
  await open(page, "#/replay");
  await page.getByTestId("replay-well").selectOption("SA-19");
  const slider = page.getByRole("slider", { name: "Well time" });
  await slider.fill(String(Math.round(Number(await slider.getAttribute("max")) * 0.8)));
  await shot(page, "10-replay-studio", true);
});

test.describe("mobile", () => {
  test.use({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });

  test("11 voice note capture, offline", async ({ page, context }) => {
    await open(page, "#/voice", "rig");
    await page.getByTestId("voice-well").selectOption("19");
    await page.getByTestId("voice-formation").selectOption("Barail");
    await context.setOffline(true);
    await page.getByTestId("voice-record").click();
    await page.waitForTimeout(1500);
    await page.getByTestId("voice-stop").click();
    await page.getByTestId("voice-save").click();
    await expect(page.getByTestId("voice-queue")).toContainText("waiting for network");
    await shot(page, "11-voice-note-mobile");
    await page.getByTestId("voice-queue").scrollIntoViewIfNeeded();
    await shot(page, "11b-voice-note-queue");
    await context.setOffline(false);
  });
});

test("12 states: stale data from this device", async ({ page }) => {
  let offline = false;
  await open(page, "#/fleet", "office", { override: { "/fleet(\\?.*)?$": (r) => (offline ? r.abort("internetdisconnected") : r.fallback()) } });
  await expect(page.getByTestId("digest")).toBeVisible();
  offline = true;
  await page.reload();
  await expect(page.getByText("Offline: showing data saved on this device")).toBeVisible();
  await shot(page, "12-state-stale");
});

test("13 states: error with retry", async ({ page }) => {
  await open(page, "#/review", "office", { override: { "/review/items(\\?.*)?$": (r) => r.fulfill({ status: 503, json: { detail: "database unavailable" } }) } });
  await expect(page.getByRole("alert")).toBeVisible();
  await shot(page, "13-state-error");
});
