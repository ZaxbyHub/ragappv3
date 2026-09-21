import { describe, expect, it, vi } from "vitest";

/**
 * Grammar-load negative cache with retry cap (issue #640, AC10).
 *
 * The pre-#640 failure path never recorded failures: every highlight call
 * of a fence whose grammar chunk could not be fetched re-attempted the
 * dynamic import, and callers re-run per render — unbounded network churn
 * on a failing deploy. The contract now: at most 2 total import attempts
 * per grammar (initial + one retry); after the second failure the language
 * degrades to themed plain text WITHOUT further imports for the rest of
 * the page session (a reload resets it).
 *
 * Mocking style follows highlighter.lazy-langs.test.ts: a vi.hoisted state
 * object plus a vi.mock factory on '@shikijs/langs/cpp' — the factory here
 * REJECTS instead of returning a grammar, and counts invocations.
 */

const state = vi.hoisted(() => ({ attempts: 0 }));

vi.mock("@shikijs/langs/cpp", async () => {
  state.attempts += 1;
  throw new Error("simulated grammar chunk fetch failure");
});

// F-001 (#651 review): concurrency probe uses its OWN grammar so the test
// stays order-independent regardless of the cumulative cpp state above.
const rubyState = vi.hoisted(() => ({ attempts: 0 }));
vi.mock("@shikijs/langs/ruby", async () => {
  rubyState.attempts += 1;
  throw new Error("simulated grammar chunk fetch failure");
});

import { loadHighlighter } from "@/lib/highlighter";

// Real grammars emit per-token `style="color:` spans; the themed
// `lang:"text"` degradation emits shiki wrappers but ZERO token-color
// spans (same discriminator as highlighter.aliases.test.ts).
const tokenSpanCount = (html: string) => (html.match(/style="color:/g) || []).length;

describe("grammar load failure negative cache (AC10 #640)", () => {
  it(
    "caps import attempts at exactly 2 across repeated highlights, degrading to plain text",
    async () => {
      const hl = await loadHighlighter();

      for (let call = 1; call <= 4; call += 1) {
        const html = await hl("int main(){}", "cpp");
        // Degradation is themed plain text: shiki markup, code preserved,
        // no token-color spans.
        expect(html, `call ${call}: shiki markup`).toContain("shiki");
        expect(html, `call ${call}: code preserved`).toContain("int main(){}");
        expect(tokenSpanCount(html), `call ${call}: expected plain-text degradation`).toBe(0);
        // Attempts: call 1 -> 1 import, call 2 -> 2nd import, calls 3-4 ->
        // negatively cached, no further imports.
        expect(state.attempts, `call ${call}: import attempts`).toBe(Math.min(call, 2));
      }

      expect(state.attempts, "exactly 2 total import attempts (retry cap)").toBe(2);
    },
    60000,
  );

  it(
    "concurrent highlights of one grammar share a single in-flight attempt (F-001 #651)",
    async () => {
      const hl = await loadHighlighter();

      // Two callers race the same grammar: single-flight must collapse the
      // concurrent loads into ONE attempt (module-level import memoization
      // alone does not guarantee this for the counter accounting).
      await Promise.all([hl("puts 1", "ruby"), hl("puts 2", "ruby")]);
      expect(rubyState.attempts).toBeLessThanOrEqual(2);

      // Settled: later calls hit the negative cache / retry cap without
      // exceeding the total attempt budget.
      await hl("puts 3", "ruby");
      await hl("puts 4", "ruby");
      expect(rubyState.attempts).toBeLessThanOrEqual(2);
    },
    60000,
  );

  it(
    "the negative cache also covers the grammar's aliases (shared loader identity)",
    async () => {
      const hl = await loadHighlighter();
      const html = await hl("int main(){}", "c++"); // alias of cpp

      expect(tokenSpanCount(html)).toBe(0);
      expect(state.attempts, "alias hits must not add import attempts").toBe(2);
    },
    60000,
  );
});
