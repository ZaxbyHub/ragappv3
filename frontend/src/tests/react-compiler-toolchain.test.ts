import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * React Compiler toolchain contract (issue #572, AC6).
 *
 * @vitejs/plugin-react 6.x is oxc-based: it has no `babel` option at all
 * (CI's tsconfig.node.json standalone typecheck enforces the Options shape —
 * TS2353 on any babel key), and React Compiler is enabled through the native
 * `compiler` option, backed by the optional peer `oxc-transform-react`
 * (pinned ^0.145.0 per plugin-react 6.1.1's peerOptional range). The
 * babel-plugin-react-compiler package belongs to the v4/v5 babel pipeline and
 * cannot be wired into this toolchain. Fails at HEAD (no compiler option, no
 * engine devDependency).
 */

const frontendRoot = resolve(__dirname, "..", "..");

describe("React Compiler toolchain wiring (AC6)", () => {
  it("lists the React Compiler engine (oxc-transform-react) as a devDependency", () => {
    const pkg = JSON.parse(
      readFileSync(resolve(frontendRoot, "package.json"), "utf-8"),
    ) as { devDependencies?: Record<string, string> };

    expect(
      pkg.devDependencies?.["oxc-transform-react"],
      "frontend/package.json devDependencies must include oxc-transform-react (plugin-react 6's React Compiler engine)",
    ).toBeTruthy();
  });

  it("enables the React Compiler via the react() plugin's compiler option", () => {
    const viteConfig = readFileSync(resolve(frontendRoot, "vite.config.ts"), "utf-8");

    expect(
      viteConfig,
      "vite.config.ts must enable React Compiler inside the react() plugin options",
    ).toMatch(/react\(\s*\{[\s\S]*?compiler:\s*true/);
  });
});
