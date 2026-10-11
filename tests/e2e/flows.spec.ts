import { expect, test, type Page } from "@playwright/test";
import { fx, mockApi, signedIn, type MockOptions } from "./mock";

// Main flow of each screen, against the captured fixtures (mock.ts). No stack needed.
test.describe.configure({ timeout: 60_000 });

async function open(page: Page, hash: string, opts: MockOptions = {}, roles?: string[]) {
  await signedIn(page, roles);
  await mockApi(page, opts);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/${hash}`);
}

/** A short mono WAV (silence with a tone), so the evidence viewer has a real recording. */
function wav(seconds = 4, rate = 8000): Buffer {
  const n = seconds * rate;
  const b = Buffer.alloc(44 + n * 2);
  b.write("RIFF", 0);
  b.writeUInt32LE(36 + n * 2, 4);
  b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20);
  b.writeUInt16LE(1, 22);
  b.writeUInt32LE(rate, 24);
  b.writeUInt32LE(rate * 2, 28);
  b.writeUInt16LE(2, 32);
  b.writeUInt16LE(16, 34);
  b.write("data", 36);
  b.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) b.writeInt16LE(Math.round(4000 * Math.sin((2 * Math.PI * 440 * i) / rate)), 44 + i * 2);
  return b;
}

test("all wells (live): public Sodir wells with the newest flagged, details on pick, basemap switch", async ({ page }) => {
  await open(page, "#/map");
  await page.getByRole("tab", { name: "All wells (live)" }).click();
  await expect(page.getByTestId("offset-map")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/2 Sodir wellbores, 1 new in 30 days/)).toBeVisible();
  const newest = page.getByRole("list", { name: "Newest public wells" });
  await expect(newest.getByRole("button").first()).toContainText("25/2-23 S");
  await expect(newest.getByRole("button").first()).toContainText("NEW");
  await newest.getByRole("button", { name: /7435\/12-1/ }).click();
  await expect(page.getByTestId("public-well")).toContainText("Lundin Norway AS");
  await expect(page.getByTestId("public-well").getByRole("link", { name: /FactPage/ })).toHaveAttribute("href", /factpages\.sodir\.no/);
  const offline = page.getByTestId("basemap").getByRole("radio", { name: "Offline" });
  await expect(offline).toBeChecked();
  await page.getByRole("button", { name: "Norwegian shelf" }).click();
});

test("offset map: analogues ranked with per-factor bars; selecting them", async ({ page }) => {
  let posted: any = null;
  await open(page, "#/map", {
    override: {
      "/offsets/select$": (r) => {
        posted = r.request().postDataJSON();
        return r.fulfill({ json: fx("offsets-select") });
      },
    },
  });
  await expect(page.getByTestId("offset-map")).toBeVisible({ timeout: 30_000 });
  const first = page.getByRole("list", { name: "Analogue wells by score" }).getByRole("button").first();
  await first.click();
  await expect(first).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("table", { name: /Analogue factors for/ })).toContainText("distance at formation");
  await page.getByRole("button", { name: /Use these \d+ offsets/ }).click();
  await expect(page.getByText(/Selection \d+ saved/)).toBeVisible();
  expect(posted).toMatchObject({ active_wellbore_id: 19, formation: "Barail" });
});

test("correlation: tracks flattened on a top; an event opens its page in the evidence viewer", async ({ page }) => {
  await open(page, "#/well/19/correlation");
  await expect(page.getByTestId("correlation-chart").locator("canvas")).toBeVisible();
  await page.getByTestId("datum-select").selectOption("Barail");
  await expect(page.getByTestId("correlation-chart")).toHaveAttribute("aria-label", /flattened on the Barail top/);
  await page.getByText("Events as a table").click();
  await page.getByRole("table", { name: "Offset events in the correlation" }).getByRole("button").first().click();
  const viewer = page.getByTestId("evidence-viewer");
  await expect(viewer.getByTestId("pdf-page").locator("canvas")).toBeVisible();
  await expect(viewer.getByRole("complementary", { name: "Extracted from this page" })).toBeVisible();
});

test("hazard roadmap: strip with the next hazard in gold and a running ETA", async ({ page }) => {
  await open(page, "#/well/19");
  await expect(page.getByTestId("strip-Barail-loss")).toBeVisible();
  await expect(page.getByTestId("strip-bit")).toBeVisible();
  const eta = page.getByTestId("eta-countdown");
  await expect(eta).toContainText("ETA");
  const a = await eta.textContent();
  await expect.poll(async () => eta.textContent(), { timeout: 5000 }).not.toBe(a);
});

test("live console: alert card, live charts, acknowledge and shelve with a reason", async ({ page }) => {
  let shelf: any = null;
  await open(page, "#/live/19", {
    override: {
      "/active/\\d+/shelves$": (r) => {
        shelf = r.request().postDataJSON();
        return r.fulfill({ json: { id: 3 } });
      },
    },
  });
  const card = page.getByTestId("alert-card");
  await expect(card).toContainText("Stuck Pipe", { ignoreCase: true });
  await expect(page.getByTestId("alert-why")).toContainText("Why:");
  for (const id of ["chart-flow", "chart-pit", "chart-torque", "chart-spp", "td-envelope", "pressure-window", "rig-state-timeline"]) await expect(page.getByTestId(id)).toBeVisible();
  await card.getByRole("button", { name: "Acknowledge" }).click();
  await expect(card).toContainText("Acknowledged");
  await card.getByRole("button", { name: "Shelve…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Reason (required)").fill("Pit transfer");
  await dialog.getByRole("button", { name: "Shelve" }).click();
  await expect(dialog).toContainText("Shelved");
  expect(shelf).toMatchObject({ hazard: "stuck_pipe", reason: "Pit transfer" });
});

test("evidence viewer: a voice-note citation plays from the cited second", async ({ page }) => {
  const audioAnswer = {
    ...fx<any[]>("ask")[0],
    question: "Voice?",
    answer: "A field note says losses at the Barail top [E9].",
    citations: [{ id: "E9", kind: "record", direct: true, type: "audio", audio_note_id: 5, uri: "s3://raw/voice/SA-19-E1.wav", t_start: 1.5, t_end: 3, synthetic: true }],
  };
  await open(page, "#/ask", {
    override: {
      "/assistant/ask/stream$": (r) => {
        const ev = (e: string, d: unknown) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`;
        return r.fulfill({ contentType: "text/event-stream", body: ev("delta", { text: audioAnswer.answer }) + ev("done", audioAnswer) });
      },
      "/audio-notes/5/file$": (r) => r.fulfill({ body: wav(), contentType: "audio/wav" }),
      "/audio-notes/5$": (r) => r.fulfill({ json: { id: 5, duration_s: 4, speaker: "mud engineer (synthetic voice)", lang: "en", well: "SA-19", formation: "Barail", spans: [{ source_ref_id: 1, t_start: 1.5, t_end: 3, text: "We had losses at the Barail top.", review_status: "pending" }] } }),
    },
  });
  await page.getByRole("textbox", { name: "Question" }).fill("Voice?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await page.getByTestId("citations").getByRole("button").first().click();
  const audio = page.getByTestId("audio");
  await expect(audio).toBeVisible();
  await expect.poll(() => audio.evaluate((a: HTMLAudioElement) => a.currentTime)).toBeCloseTo(1.5, 1);
  await expect(page.getByTestId("audio-evidence")).toContainText("We had losses at the Barail top.");
});

test("ask WellMind: streamed answer, evidence drawer, list of wells as table and map", async ({ page }) => {
  await open(page, "#/ask");
  await page.getByRole("button", { name: "Which wells had losses in the Barail?" }).click();
  await expect(page.getByTestId("answer")).toContainText("loss events in the Barail");
  const wells = page.getByTestId("wells-result");
  await expect(wells.getByRole("table")).toBeVisible();
  expect(await wells.getByRole("row").count()).toBeGreaterThan(5);
  await expect(wells.getByRole("group", { name: /Map of the \d+ wells/ })).toBeVisible();
  await page.getByRole("button", { name: /Show evidence \(\d+\)/ }).click();
  await expect(page.getByRole("dialog", { name: "Evidence behind this answer" })).toContainText("[E1]");
});

test("program linter: upload a CSV, findings by formation with offsets, briefing PDF", async ({ page }) => {
  await open(page, "#/plan/19");
  await page.getByTestId("program-upload").setInputFiles({
    name: "sa19.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("name,md_top,md_base,hole_in,casing_od_in,mud_type,mw_sg_min,mw_sg_max\nsurface,0,500,17.5,13.375,wbm,1.05,1.1\nproduction,500,3622,8.5,7,wbm,1.15,1.35\n"),
  });
  await expect(page.getByText("uploaded sa19.csv")).toBeVisible();
  await expect(page.getByLabel("Section 2 name")).toHaveValue("production");
  await page.getByRole("button", { name: "Check program" }).click();
  await expect(page.getByTestId("lint-row-Barail")).toContainText("Fail");
  await expect(page.getByTestId("lint-row-Barail")).toContainText("L-SEAT-LOSS");
  await expect(page.getByTestId("lint-row-Dhekiajuli")).toContainText("Pass");
  await page.getByRole("button", { name: "Write briefing" }).click();
  await expect(page.getByTestId("briefing")).toContainText("Pre-spud hazard briefing");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download PDF" }).click();
  expect((await download).suggestedFilename()).toMatch(/briefing-\d+\.pdf$/);
});

test("program linter: a CSV with a missing column is refused with the reason", async ({ page }) => {
  await open(page, "#/plan/19");
  await page.getByTestId("program-upload").setInputFiles({ name: "bad.csv", mimeType: "text/csv", buffer: Buffer.from("name,md_top\nx,0\n") });
  await expect(page.getByRole("alert")).toContainText("missing column");
});

test("review console: keyboard J/K moves, A accepts, conflicts are highlighted", async ({ page }) => {
  const decided: any[] = [];
  await open(page, "#/review", {
    override: {
      "/review/items/\\d+$": (r) => {
        decided.push({ url: r.request().url(), body: r.request().postDataJSON() });
        return r.fulfill({ json: { status: "accepted" } });
      },
    },
  });
  const items = fx<any[]>("review").filter((i) => i.status === "open");
  await expect(page.getByTestId(`queue-${items[0].id}`)).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("review-conflicts")).toBeVisible();
  await page.keyboard.press("j");
  await expect(page.getByTestId(`queue-${items[1].id}`)).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("k");
  await expect(page.getByTestId(`queue-${items[0].id}`)).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("a");
  await expect(page.getByTestId("review-said")).toContainText("accepted");
  expect(decided[0].url).toContain(`/review/items/${items[0].id}`);
  expect(decided[0].body.action).toBe("accept");
});

test("fleet board: rigs most urgent first, next hazard ETA, shift digest", async ({ page }) => {
  await open(page, "#/fleet");
  const list = page.getByRole("list", { name: "Rigs, most urgent first" });
  await expect(list.getByRole("listitem").first()).toBeVisible();
  expect(await list.getByRole("listitem").count()).toBe(fx("fleet").rigs.length);
  await expect(page.getByTestId("rig-SA-19")).toContainText("Next hazard");
  await expect(page.getByTestId("digest")).toContainText("Raised");
});

test("replay studio: play a held-out well, see the measured result", async ({ page }) => {
  await open(page, "#/replay");
  await page.getByTestId("replay-well").selectOption("SA-19");
  await page.getByTestId("replay-speed").selectOption("600");
  await page.getByTestId("replay-play").click();
  const time = page.getByRole("slider", { name: "Well time" });
  await expect.poll(async () => Number(await time.inputValue())).toBeGreaterThan(300);
  await time.fill(String(await time.getAttribute("max")));
  await expect(page.getByTestId("replay-results")).toContainText("Replay finished.");
  await expect(page.getByTestId("replay-results")).not.toContainText("…");
});

test.describe("voice note", () => {
  test("recorded offline, queued on the device, uploaded when back online", async ({ page, context }) => {
    const uploaded: string[] = [];
    await open(page, "#/voice", {
      override: {
        "/voice-notes(\\?.*)?$": (r) => {
          if (r.request().method() === "POST") {
            uploaded.push(r.request().url());
            return r.fulfill({ status: 201, json: { id: 901, status: "pending_confirmation", duplicate: false } });
          }
          return r.fulfill({
            json: uploaded.length ? [{ id: 901, well: "SA-19", formation: "Barail", note: "tight at the coal", duration_s: 1.2, recorded_at: new Date().toISOString(), status: "pending_confirmation", lessons: 1, open_review_items: 1 }] : [],
          });
        },
      },
    });
    await page.getByTestId("voice-well").selectOption("19");
    await page.getByTestId("voice-formation").selectOption("Barail");
    await page.getByLabel("Short note (optional)").fill("tight at the coal");
    await context.setOffline(true);
    await page.getByTestId("voice-record").click();
    await page.waitForTimeout(1200);
    await page.getByTestId("voice-stop").click();
    await page.getByTestId("voice-save").click();
    await expect(page.getByTestId("voice-queue")).toContainText("waiting for network");
    expect(uploaded).toHaveLength(0);
    await context.setOffline(false);
    await expect(page.getByTestId("voice-queue")).toContainText("Nothing waiting");
    expect(uploaded[0]).toContain("formation=Barail");
    await expect(page.getByTestId("voice-uploaded")).toContainText("Pending expert confirmation");
  });
});


test("roles: each role sees only its own actions (the API enforces the same rule)", async ({ page }) => {
  await open(page, "#/review", {}, ["planner"]);
  await expect(page.getByText("Deciding needs the data steward role; you can browse the queue.")).toBeVisible();
  await page.goto("/#/live/19");
  await expect(page.getByTestId("alert-card")).toBeVisible();
  await expect(page.getByTestId("alert-card").getByRole("button", { name: "Acknowledge" })).toHaveCount(0);
  await page.goto("/#/admin");
  await expect(page.getByText("This page needs the admin role.")).toBeVisible();
  await page.goto("/#/voice");
  await expect(page.getByText("Recording notes needs the wellsite engineer or superintendent role.")).toBeVisible();
});
