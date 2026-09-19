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
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: [
    {
      command: "node stub-backend.mjs",
      url: "http://localhost:9090/api/health",
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
