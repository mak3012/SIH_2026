import { expect, test } from "@playwright/test";

// The live view must turn grey within 10 s when the feed goes stale. The WebSocket is mocked,
// so this runs against the web app alone (vite dev server), no API or database needed.
test("stale feed turns the live view grey within 10 s", async ({ page }) => {
  await page.routeWebSocket(/\/active\/7\/stream$/, (ws) => {
    ws.onMessage((m) => {
      if (JSON.parse(String(m)).type !== "auth") return;
      ws.send(JSON.stringify({ type: "snapshot", wellbore_id: 7, feed: { state: "live" }, stale_after_s: 6,
        sample: { ts: new Date().toISOString(), rig_state: "drilling_rotary", values: { bit_md: 1500 } }, alerts: [] }));
      const sample = (i: number) => ws.send(JSON.stringify({ type: "sample", ts: new Date().toISOString(),
        rig_state: "drilling_rotary", formation: "Barail", values: { bit_md: 1500 + i, spp: 180 } }));
      let i = 0;
      const timer = setInterval(() => (++i <= 3 ? sample(i) : clearInterval(timer)), 500); // then silence
    });
  });
  // Signed in (the live view needs a user); the token is only displayed, the mock never checks it.
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  const tok = `${b64({ alg: "none" })}.${b64({ preferred_username: "engineer", realm_access: { roles: ["wellsite_engineer"] } })}.sig`;
  await page.addInitScript((t) => {
    sessionStorage.setItem("oidc", JSON.stringify({ access_token: t, expires_at: Date.now() + 3_600_000 }));
    sessionStorage.setItem("access_token", t);
  }, tok);
  await page.goto("/#/live/7");
  const panel = page.getByTestId("live-well");
  await expect(panel).toHaveAttribute("data-feed-state", "live", { timeout: 30_000 }); // first load compiles the page
  await expect(page.getByTestId("feed-state")).toHaveText(/live/i);
  const silentFrom = Date.now();
  await expect(panel).toHaveAttribute("data-feed-state", "stale", { timeout: 10_000 });
  expect(Date.now() - silentFrom).toBeLessThan(10_000);
  await expect(page.getByTestId("feed-state")).toHaveText(/data stale/i);
  await expect(panel).toHaveCSS("filter", "grayscale(1)");
});
