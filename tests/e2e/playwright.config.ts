import { defineConfig } from "@playwright/test";

// Default: the web dev server (started if not running) with the API mocked from fixtures.
// STACK=1: against the running stack (`make demo`) over HTTPS with its private CA
// (stack.spec.ts and the API health check); override targets via WEB_URL / API_URL.
const stack = !!process.env.STACK;
export default defineConfig({
  testDir: ".",
  expect: { timeout: 10_000 },
  // One retry: on a laptop that also runs the stack, WebGL and timing tests can miss a deadline
  // under load. A test that passes on retry is still reported as flaky.
  retries: 1,
  // PW_CHANNEL=msedge uses the installed Edge instead of a downloaded Chromium.
  // A fake microphone, so voice capture can be tested headless.
  use: {
    // The mocked tests serve the app on 5174, so they also run while the stack holds 5173.
    baseURL: process.env.WEB_URL ?? (stack ? "https://localhost:5173" : "http://localhost:5174"),
    ignoreHTTPSErrors: stack,
    channel: process.env.PW_CHANNEL,
    launchOptions: { args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] },
    permissions: ["microphone"],
  },
  webServer: stack
    ? undefined
    : {
        command: "npm run dev -w apps/web -- --port 5174 --strictPort",
        cwd: "../..",
        url: "http://localhost:5174",
        reuseExistingServer: true,
      },
});
