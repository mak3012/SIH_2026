import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

// The demo (docs/demo.md) against the running stack, through Keycloak and TLS: no mocks.
// After `make demo` (or `.\demo.ps1`):  STACK=1 npm run e2e -- stack.spec.ts
// Uses the dev realm user `planner` (infra/keycloak/realm.json).
test.skip(!process.env.STACK, "needs the running stack: STACK=1");
test.describe.configure({ timeout: 180_000 });

async function signIn(page: Page) {
  await page.goto("/#/ask");
  await page.getByRole("button", { name: "Sign in" }).first().click();
  await page.locator("#username").fill("planner");  // Keycloak's login form
  await page.locator("#password").fill("planner-dev-password");
  await page.locator("#kc-login").click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1_000); // the sign-in callback settles on its landing page first
}

test("a returning visitor (service worker installed) still reaches Keycloak's sign-in page", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  await page.getByRole("button", { name: "Sign in" }).first().click();
  await expect(page.locator("#username")).toBeVisible({ timeout: 15_000 });
});

test("sign up: a new account lands signed in as a read-only viewer", async ({ page }) => {
  const name = `judge${Date.now()}`;
  await page.goto("/#/ask");
  await page.getByRole("button", { name: "Sign up" }).click();
  await page.locator("#username").fill(name);  // Keycloak's registration form
  await page.locator("#email").fill(`${name}@example.com`);
  await page.locator("#firstName").fill("Demo");
  await page.locator("#lastName").fill("Judge");
  await page.locator("#password").fill("judge-dev-password");
  await page.locator("#password-confirm").fill("judge-dev-password");
  await page.locator("input[type=submit]").click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("banner")).toContainText("viewer");
  // Reads work for the demo blocks; actions stay with the roles an administrator grants.
  const id = await wellboreId(page, "SA-26");
  expect(id).toBeGreaterThan(0);
});

test("step 1: a scanned page becomes a fact, with its box and the depth conversion", async ({ page }) => {
  await signIn(page);
  await page.goto("/#/ask");
  await page.getByRole("textbox", { name: "Question", exact: true }).fill("Where is the Tipam top in SA-26?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByTestId("answer")).toContainText("1,117.1 m MD", { timeout: 60_000 });
  await page.getByTestId("citations").getByRole("button").first().click();
  const viewer = page.getByTestId("evidence-viewer");
  await expect(viewer.getByTestId("pdf-page").locator("canvas")).toBeVisible({ timeout: 30_000 });
  await expect(viewer.getByTestId("depth-conversion").first()).toContainText("ft");
});

test("step 3: the linter blocks the SA-26 program on the Barail", async ({ page }) => {
  await signIn(page);
  const id = await wellboreId(page, "SA-26");
  await selectOffsets(page, id, "Tipam");
  await page.goto(`/#/plan/${id}`);
  await page.getByTestId("program-upload").setInputFiles(join(__dirname, "../../docs/demo/sa-26-program.csv"));
  await page.getByRole("button", { name: "Check program" }).click();
  await expect(page.getByTestId("lint-row-Barail")).toContainText("L-SEAT-LOSS", { timeout: 60_000 });
});

test("step 4: the replayed well's alert, with the offsets' source pages", async ({ page }) => {
  await signIn(page);
  const id = await wellboreId(page, process.env.DEMO_WELL ?? "SA-06");
  await page.goto(`/#/live/${id}`);
  const card = page.getByTestId("alert-card");
  await expect(card).toContainText(/loss/i, { timeout: 60_000 });
  await expect(card.getByRole("button").filter({ hasText: /SA-|p\./ }).first()).toBeVisible();
});

test("step 5: a cited answer about the Barail", async ({ page }) => {
  await signIn(page);
  await page.goto("/#/ask");
  await page.getByRole("textbox", { name: "Question", exact: true }).fill("What went wrong in the Barail on SA-05, SA-06 and SA-16?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByTestId("answer")).toContainText("SA-06", { timeout: 90_000 });
  await expect(page.getByTestId("citations").getByRole("button").first()).toBeVisible();
});

// ------------------------------------------------------------------ helpers (API with the page's token)
async function token(page: Page): Promise<string> {
  return page.evaluate(() => sessionStorage.getItem("access_token") ?? "");
}

async function api<T>(page: Page, path: string, init?: { method: string; data: unknown }): Promise<T> {
  const base = process.env.API_URL ?? "https://localhost:5173/api";
  const headers = { Authorization: `Bearer ${await token(page)}` };
  const res = init ? await page.request.post(`${base}${path}`, { headers, data: init.data }) : await page.request.get(`${base}${path}`, { headers });
  expect(res.ok(), `${path}: ${res.status()} ${await res.text()}`).toBeTruthy();
  return (await res.json()) as T;
}

async function wellboreId(page: Page, well: string): Promise<number> {
  const rows = await api<{ wellbore_id: number; well: string }[]>(page, "/wellbores");
  const row = rows.find((r) => r.well === well);
  expect(row, `${well} in /wellbores`).toBeTruthy();
  return row!.wellbore_id;
}

async function selectOffsets(page: Page, id: number, formation: string) {
  await api(page, "/offsets/select", {
    method: "POST",
    data: { active_wellbore_id: id, formation, surface_radius_m: 30000, subsurface_radius_m: 25000 },
  });
}

test("all wells (live): the synced Sodir wells on the map with the newest first", async ({ page }) => {
  await signIn(page);
  const wells = await api<{ name: string; lat: number; entry_date: string | null }[]>(page, "/public-wells");
  test.skip(wells.length === 0, "public wells not synced (make public-wells needs internet)");
  expect(wells.length).toBeGreaterThan(1000);
  await page.goto("/#/map");
  await page.getByRole("tab", { name: "All wells (live)" }).click();
  await expect(page.getByText(/Sodir wellbores/)).toBeVisible({ timeout: 30_000 });
  const newest = wells.filter((w) => w.entry_date).sort((a, b) => b.entry_date!.localeCompare(a.entry_date!))[0];
  await expect(page.getByRole("list", { name: "Newest public wells" }).getByRole("button").first()).toContainText(newest.name);
  await expect(page.getByTestId("offset-map").locator("canvas").first()).toBeVisible();
});

// Keep the CSV the script uploads parseable (a typo would only show on demo day).
test("the demo program CSV has the template's columns", () => {
  const head = readFileSync(join(__dirname, "../../docs/demo/sa-26-program.csv"), "utf-8").split(/\r?\n/)[0];
  expect(head).toBe("name,md_top,md_base,hole_in,casing_od_in,mud_type,mw_sg_min,mw_sg_max");
});
