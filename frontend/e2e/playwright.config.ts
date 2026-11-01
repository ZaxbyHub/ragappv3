// frontend/e2e/playwright.config.ts — issue #573 (AC7) acceptance check C7.
//
// Boots two servers:
//   1. stub-backend.mjs — zero-dependency node:http stub on :9090 implementing
//      the minimum REAL frontend client contract (auth + CSRF + vaults +
//      chat sessions + durable-turn SSE chat stream; see stub-backend.mjs).
//   2. the app itself — `npx vite preview --port 4173 --strictPort` run from
//      frontend/ (cwd below). The implementer adds a `preview` proxy block to
//      frontend/vite.config.ts (mirroring the dev-server proxy in
//      vite.paths.ts createApiProxy) so /api on :4173 forwards to :9090.
//      NOTE: `vite preview` serves dist/ — run the frontend build before this
//      suite (the repo's `npm run build`).
import { defineConfig } from "@playwright/test";
import path from "node:path";

// E2E_STUB_PORT (issue #781): local runs can move the stub off :9090 (a
// foreign service may squat it). stub-backend.mjs and the vite preview proxy
// (frontend/vite.paths.ts) read the same variable, so the whole tier moves
// together; CI never sets it and stays on the default.
const stubPort = process.env.E2E_STUB_PORT || "9090";

export default defineConfig({
  testDir: ".",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["line"]],
  use: {
    baseURL: "http://localhost:4173",
    // Bound un-timed waits across the e2e tier: the app polls /api/health,
    // so a "networkidle" wait may never settle — without bounds a spec
    // could hang until the test timeout with no record written.
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: [
    {
      command: "node stub-backend.mjs",
      url: `http://localhost:${stubPort}/api/health`,
      timeout: 30_000,
      reuseExistingServer: true,
    },
    {
      command: "npx vite preview --port 4173 --strictPort",
      cwd: path.resolve(__dirname, ".."),
      url: "http://localhost:4173",
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
