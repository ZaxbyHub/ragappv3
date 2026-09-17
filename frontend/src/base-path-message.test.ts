import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { normalizeBasePath } from "../vite.paths";
import { normalizeBasePath as normalizeBasePathLib } from "./lib/normalize-base-path";

// Issue #567: Git Bash's MSYS layer rewrites leading-slash env values into
// Windows paths (e.g. VITE_APP_BASENAME=/knowledgevault arrives as
// "C:/Program Files/Git/knowledgevault"), and the validators correctly reject
// that value — but the thrown message must name the received value so the
// developer can see the rewrite happened. The validation logic is duplicated
// across vite.paths.ts (config-time), src/lib/normalize-base-path.ts
// (runtime), and frontend/Dockerfile (image build); every copy must
// interpolate the received value.
const MSYS_REWRITTEN = "C:/Program Files/Git/knowledgevault";

const cases: Array<[label: string, input: string, fragment: string]> = [
  ["unsafe characters (MSYS-rewritten path)", MSYS_REWRITTEN, MSYS_REWRITTEN],
  ["leading whitespace", " /knowledgevault", ' /knowledgevault'],
  ["URL", "https://example.com/kv", "https://example.com/kv"],
  ["duplicate slashes", "/kv//admin", "/kv//admin"],
  ["relative segments", "/kv/../admin", "/kv/../admin"],
];

describe.each([
  ["src/lib/normalize-base-path.ts", normalizeBasePathLib],
  ["vite.paths.ts", normalizeBasePath],
])("base-path error diagnostics (%s)", (_label, normalize) => {
  it.each(cases)('includes the received value in the "%s" error', (_case, input, fragment) => {
    expect(() => normalize(input)).toThrow(
      expect.objectContaining({ message: expect.stringContaining(fragment) }),
    );
  });

  it("still rejects the MSYS-rewritten value (behavior unchanged)", () => {
    expect(() => normalize(MSYS_REWRITTEN)).toThrow();
  });

  it("still accepts a clean basename (behavior unchanged)", () => {
    expect(normalize("/knowledgevault")).toBe("/knowledgevault");
  });
});

describe("base-path error diagnostics (frontend/Dockerfile copy)", () => {
  // The Dockerfile validator runs at image-build time inside `RUN node -e`,
  // so it cannot be imported like the TS copies. The keep-in-sync contract is
  // asserted at the source level instead: every VITE_APP_BASENAME validation
  // throw must concatenate JSON.stringify(raw) so the received value is
  // visible in build logs.
  // Location-invariant (any cwd), and deliberately NOT
  // `new URL(relative, import.meta.url)`: vite statically rewrites that
  // asset pattern to a non-file URL, which breaks readFileSync under vitest.
  const dockerfile = readFileSync(
    resolve(fileURLToPath(import.meta.url), "../../Dockerfile"),
    "utf-8",
  );

  it("interpolates the received value in every validation throw", () => {
    const allThrows = dockerfile.match(/throw new Error\('VITE_APP_BASENAME[^']*/g) ?? [];
    expect(allThrows.length).toBeGreaterThan(0);
    const interpolated = dockerfile.match(
      /throw new Error\('VITE_APP_BASENAME[^']*'\s*\+\s*JSON\.stringify\(raw\)/g,
    );
    expect(interpolated).toHaveLength(allThrows.length);
  });
});
