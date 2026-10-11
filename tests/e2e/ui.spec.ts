import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

// The web app against a mocked API (no stack needed): login redirect, evaluation numbers,
// assistant answers with citations and evidence, roadmap after selecting offsets.

const API = "http://localhost:8000";

function fakeToken(roles = ["wellsite_engineer", "planner"]): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none" })}.${b64({ preferred_username: "engineer", realm_access: { roles }, exp: 4_000_000_000 })}.sig`;
}

async function signedIn(page: Page) {
  const t = fakeToken();
  await page.addInitScript((tok) => {
    sessionStorage.setItem("oidc", JSON.stringify({ access_token: tok, expires_at: Date.now() + 3_600_000 }));
    sessionStorage.setItem("access_token", tok);
  }, t);
}

test("sign in starts an authorization-code + PKCE redirect to Keycloak", async ({ page }) => {
  let authUrl = "";
  await page.route("http://localhost:8080/**", (route) => {
    authUrl = route.request().url();
    return route.fulfill({ body: "keycloak" });
  });
  await page.goto("/#/ask");
  await page.getByRole("button", { name: "Sign in" }).first().click();
  await expect.poll(() => authUrl).toContain("/realms/wells/protocol/openid-connect/auth");
  const q = new URL(authUrl).searchParams;
  expect(q.get("client_id")).toBe("web");
  expect(q.get("code_challenge_method")).toBe("S256");
  expect(q.get("code_challenge")?.length).toBeGreaterThan(40);
});

test("sign up opens Keycloak's registration form with the same PKCE parameters", async ({ page }) => {
  let authUrl = "";
  await page.route("http://localhost:8080/**", (route) => {
    authUrl = route.request().url();
    return route.fulfill({ body: "keycloak" });
  });
  await page.goto("/#/ask");
  await page.getByRole("button", { name: "Sign up" }).click();
  await expect.poll(() => authUrl).toContain("/realms/wells/protocol/openid-connect/registrations");
  const q = new URL(authUrl).searchParams;
  expect(q.get("client_id")).toBe("web");
  expect(q.get("code_challenge_method")).toBe("S256");
});

test("replay page shows the measured numbers from metrics.json", async ({ page }) => {
  const m = JSON.parse(readFileSync(join(__dirname, "../../docs/eval-report/metrics.json"), "utf-8")).metrics;
  await page.goto("/#/replay");
  const box = page.getByTestId("alert-metrics");
  await expect(box).toContainText(`${(m.alert_event_recall_warning * 100).toFixed(0)} %`);
  await expect(page.getByText("synthetic data")).toBeVisible();
});

test("assistant answer shows citations, evidence and follow-ups", async ({ page }) => {
  await signedIn(page);
  const asked: string[] = [];
  await page.route(`${API}/assistant/ask/stream`, async (route) => {
    const body = route.request().postDataJSON();
    asked.push(body.question);
    const answer = {
      conversation_id: 3,
      question: body.question,
      status: "answered",
      answer: "In SA-19 the Barail top is at 2,153.1 m MD [E1].",
      citations: [{ id: "E1", kind: "record", direct: true, type: "document", document: "SA-19 WCR (SYNTHETIC)", page: 2, bbox: [0.07, 0.18, 0.69, 0.19], synthetic: true }],
      evidence: [{ id: "E1", kind: "record", text: "SA-19: Barail top at 2,153.1 m MD", values: {}, query: { view: "v_tops", sql: "SELECT * FROM chat.v_tops", params: {} } }],
      followups: ["What drilling problems were recorded in the Barail?"],
      refusal: null,
    };
    const ev = (e: string, d: unknown) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`;
    await route.fulfill({ contentType: "text/event-stream", body: ev("status", { stage: "answering" }) + ev("delta", { text: answer.answer }) + ev("done", answer) });
  });
  await page.goto("/#/ask");
  await page.getByRole("textbox", { name: "Question" }).fill("What is the Barail top in SA-19?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByTestId("answer")).toHaveText(/2,153\.1 m MD/);
  await expect(page.getByTestId("citations")).toContainText("SA-19 WCR (SYNTHETIC) p. 2");
  await page.getByText("Show evidence (1)").click();
  await page.getByText("Query on chat.v_tops").click();
  await expect(page.getByText("SELECT * FROM chat.v_tops")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "What drilling problems were recorded in the Barail?" }).click();
  await expect.poll(() => asked.length).toBe(2);
});

test("a well without offsets asks for them, then shows the roadmap", async ({ page }) => {
  await signedIn(page);
  let selected = false;
  await page.route(`${API}/wellbores`, (r) =>
    r.fulfill({ json: [{ wellbore_id: 7, well: "SA-07", wellbore: "SA-07", block: "SA-BLK-1", status: "completed", td_md: 3000, has_selection: false, feed_state: null, open_alerts: 0 }] }),
  );
  await page.route(`${API}/wellbores/7/formations`, (r) => r.fulfill({ json: [{ formation: "Tipam", top_md: 900 }, { formation: "Barail", top_md: 1600 }] }));
  await page.route(`${API}/offsets/select`, (r) => {
    selected = true;
    return r.fulfill({ json: { selection_id: 1, candidates: [] } });
  });
  await page.route(`${API}/active/7/hazard-profile`, (r) =>
    selected
      ? r.fulfill({
          json: {
            selection_id: 1,
            offsets: [{ well: "SA-05", weight: 0.62 }],
            formations: [{ formation: "Tipam", top_md: 900, base_md: 1600 }, { formation: "Barail", top_md: 1600, base_md: 2400 }],
            cells: [{ formation: "Barail", hazard: "loss", mean: 0.41, ci90: [0.2, 0.63], n_eff: 4.2, counts: { event: 2, none: 2, unknown: 1, not_reached: 0 }, evidence_state: "evidence", band_md: [1610, 1680] }],
          },
        })
      : r.fulfill({ status: 409, json: { detail: "no offset selection for this wellbore" } }),
  );
  await page.route(`${API}/active/7/lookahead`, (r) => r.fulfill({ json: { current_md: 1500, rate_m_per_hr: 12, rate_basis: "realtime", bands: [] } }));
  await page.route(`${API}/active/7/alerts`, (r) => r.fulfill({ json: [] }));
  await page.goto("/#/well/7");
  await expect(page.getByText("No offset wells selected yet")).toBeVisible();
  await page.getByRole("tab", { name: "Offsets" }).click();
  await page.getByRole("button", { name: "Select offsets" }).click();
  await page.getByRole("tab", { name: "Roadmap" }).click();
  await expect(page.getByTestId("cell-Barail-loss")).toContainText("41 %");
  await expect(page.getByText("2 event · 2 none · 1 unknown")).toBeVisible();
});
