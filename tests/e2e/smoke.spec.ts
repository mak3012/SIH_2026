import { expect, test } from "@playwright/test";

test("web shell renders", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Well Intelligence" })).toBeVisible();
});

test("api is healthy", async ({ request }) => {
  test.skip(!process.env.STACK, "needs the running stack: STACK=1");
  const res = await request.get(`${process.env.API_URL ?? "https://localhost:5173/api"}/health`);
  expect(await res.json()).toEqual({ status: "ok" });
});

test("rig theme: dark, 48 px touch targets, kept across reloads, keyboard reachable", async ({ page }) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: /theme; switch to Rig/ });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "rig");
  const box = await page.getByRole("button", { name: /theme; switch to Office/ }).boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(48);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "rig");
});
