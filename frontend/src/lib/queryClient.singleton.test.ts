// Structural pin (issue #1032, implementation-review nit): main.tsx must
// render the provider with the SHARED singleton from @/lib/queryClient —
// the identity-boundary reset clears THAT instance, so a regression back to
// a module-local `new QueryClient()` in main.tsx would silently disconnect
// the boundary from the cache actually backing the app.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

describe("app QueryClient singleton wiring (#1032)", () => {
  it("main.tsx imports the shared client and constructs no local one", () => {
    const mainPath = path.join(
      path.dirname(fileURLToPath(import.meta.url)),
      "..",
      "main.tsx",
    );
    const src = readFileSync(mainPath, "utf8");
    expect(src).toMatch(/import \{ queryClient \} from ['"]@\/lib\/queryClient['"]/);
    expect(src).not.toMatch(/new QueryClient\(/);
    expect(src).toMatch(/client=\{queryClient\}/);
  });
});
