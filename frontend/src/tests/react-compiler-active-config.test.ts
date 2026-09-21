import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { extractActiveConfigSource } from "./helpers/active-config";

/**
 * React Compiler wiring is comment-immune (issue #640, AC8).
 *
 * The toolchain test's regex (/react\(\s*\{[\s\S]*?compiler:\s*true/)
 * applied to the RAW vite.config.ts matches commented-out code —
 * `[\s\S]*?` happily crosses comment boundaries, so a config with a
 * commented `// compiler: true` above an active `compiler: false` read as
 * "wired". Extracting only the active source first closes that hole.
 * The current tree has an ACTIVE `compiler: true` and no commented
 * duplicate, so the weakness is demonstrated on fixtures (per the #640
 * reproduction trace) while the real config is pinned through the same
 * helper.
 */

const frontendRoot = resolve(__dirname, "..", "..");
const wiringRegex = /react\(\s*\{[\s\S]*?compiler:\s*true/;

/** Fixture shaped like vite.config.ts with the compiler DISABLED and a
 *  commented-out ENABLED line above it — the exact false-positive shape. */
const commentedOutEnabledFixture = `
import react from '@vitejs/plugin-react';

export default defineConfig(() => ({
  plugins: [
    react({
      // compiler: true   <- disabled during migration, keep off
      compiler: false,
    }),
  ],
}));
`;

/** Same shape, genuinely enabled (the current real config's shape). */
const activeEnabledFixture = `
import react from '@vitejs/plugin-react';

export default defineConfig(() => ({
  plugins: [
    react({
      compiler: true,
    }),
  ],
}));
`;

describe("React Compiler wiring is read from active source only (AC8)", () => {
  it("a commented-out `compiler: true` above an active `compiler: false` is NOT wired", () => {
    const active = extractActiveConfigSource(commentedOutEnabledFixture);

    // The raw source DOES match — that is the latent weakness being pinned.
    expect(commentedOutEnabledFixture).toMatch(wiringRegex);

    // The extracted active source must not.
    expect(
      active,
      "commented-out compiler: true must not read as wired after comment stripping",
    ).not.toMatch(wiringRegex);
    expect(active).toContain("compiler: false");
  });

  it("an active `compiler: true` still reads as wired", () => {
    const active = extractActiveConfigSource(activeEnabledFixture);

    expect(active).toMatch(wiringRegex);
  });

  it("the real vite.config.ts reads as wired through the helper", () => {
    const raw = readFileSync(resolve(frontendRoot, "vite.config.ts"), "utf-8");
    const active = extractActiveConfigSource(raw);

    expect(active, "React Compiler must actually be enabled in vite.config.ts").toMatch(
      wiringRegex,
    );
  });
});
