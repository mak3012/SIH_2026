import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// - The evaluation numbers come from docs/eval-report/*.json (outside this app): the dev server
//   may read from the repository root.
// - PWA: the app shell is precached; hazard packages (roadmap, lookahead, alerts, 3D scene of a
//   well) are cached in IndexedDB by the app itself (src/offline.ts), so Rig mode works offline.
// - three.js and the 3D scene load only when a 3D view opens (separate chunks).
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: "auto",
      manifest: {
        name: "Well Intelligence",
        short_name: "Wells",
        description: "Drilling records, hazard roadmap, live alerts and cited answers.",
        theme_color: "#0B3C5D",
        background_color: "#05080F",
        display: "standalone",
        start_url: "/",
        icons: [{ src: "/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any maskable" }],
      },
      workbox: {
        globPatterns: ["**/*.{js,mjs,css,html,svg,woff2}"],
        // Replay timelines (one chunk per well) are desk material: fetched when opened, not
        // pushed to every rig tablet.
        globIgnores: ["**/assets/SA-*.js"],
        navigateFallback: "/index.html",
        // Keycloak (/auth: sign-in, sign-up) and the API (/api/docs) share the origin: their pages
        // must come from the network, never be answered with the app shell.
        navigateFallbackDenylist: [/^\/auth\//, /^\/api\//],
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
      },
    }),
  ],
  server: { fs: { allow: ["../.."] } },
  build: {
    rollupOptions: {
      output: {
        manualChunks: (id) => (id.includes("node_modules/three") ? "three" : undefined),
      },
    },
  },
});
