// frontend/src/index.css.contrast.test.ts
// Issue #777 (AC7 / UI-ENH-12) — the token-contrast guardrail. Computes the
// WCAG 2.x contrast ratio for every text/background token pair the design
// system implies, in the light (:root), dark (.dark) and high-contrast
// (.high-contrast) themes parsed live from src/index.css, and asserts every
// pair is >= 4.5:1 (WCAG 1.4.3 AA). RED at the pre-fix tokens (dark
// destructive-foreground 3.49, dark success-foreground 2.10, dark
// success-subdued-foreground 3.79, dark warning-foreground 2.17, and light
// destructive-on-background 4.40 — the muted-foreground-on-input 4.44 RED
// belongs to the frozen C5 check, not this file, because that pair's only
// text consumer moved to bg-muted; see exclusion 2); GREEN after the
// #777 token moves. Asserting the FULL-opacity composited token values is
// deliberate: components layer /NN opacity modifiers over these pairs, and
// an alpha modifier over a lighter surface can only lower contrast — the
// exact mistake the input.tsx placeholder comment documented in reverse
// before #777 corrected it.
//
// Pair scope (25 pairs x 3 themes):
//   - every X-foreground token on its own X surface (the system's pairing
//     convention), plus foreground-family pairs on their base surfaces;
//   - muted-foreground on every surface it is drawn over (muted, card,
//     background, popover);
//   - accent-foreground on input (the outline Button's hover surface);
//   - the status hues (primary/destructive/success/warning) as TEXT on card
//     and background, the surfaces they are actually painted on.
//
// Documented exclusions (each re-checked by the Phase 4.2 sweep predicates,
// see the #777 trace's 08a):
//   1. --filetype-* tokens paint only decorative aria-hidden icons
//      (lib/fileIcon.tsx is the sole consumer) — not text/background pairs
//      under WCAG 1.4.3. Re-include the day they color real text.
//   2. muted-foreground on --input: its only text consumer was TabsList,
//      which #777 moved to bg-muted. --input remains the Input border and
//      the outline Button hover surface; that text pair
//      (accent-foreground on input) IS checked below. In the high-contrast
//      theme --input is pure black for the border affordance and provably
//      cannot host both muted-foreground (needs surface luminance >= 0.324)
//      and accent-foreground (needs <= 0.1833) — empty window.
//   3. destructive/success/warning on --muted (4.25-4.39 with the #777
//      tokens): no element paints status text on a solid muted surface —
//      28 non-test files contain both token families but ZERO same-class-
//      string co-occurrences (the codebase pairs status text with its own
//      hue wash or card/popover/background; solid bg-muted carries only
//      muted-foreground/foreground text). The count comes from the
//      unanchored pattern below and is a safe superset (button.tsx is a
//      -foreground-only member). Related wash margins, all passing today:
//      DocumentTable rows (hover:bg-muted/50, bg-muted/30 selected) over
//      their Card under-surface — light success 4.53 hover / 4.63 selected,
//      warning 4.64, dark destructive 4.77; WikiPageList bulk bar 5.5+.

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const cssPath = resolve(dirname(fileURLToPath(import.meta.url)), "index.css");

// =============================================================================
// Token parsing + WCAG math (sRGB relative luminance, WCAG 2.x)
// =============================================================================

type Theme = "light" | "dark" | "high-contrast";
type Rgb = [number, number, number];

function parseThemes(css: string): Record<Theme, Record<string, Rgb>> {
  const themes: Record<Theme, Record<string, Rgb>> = {
    light: {},
    dark: {},
    "high-contrast": {},
  };
  const nameOf = (sel: string): Theme | undefined =>
    sel === ":root"
      ? "light"
      : sel === ".dark"
        ? "dark"
        : sel === ".high-contrast"
          ? "high-contrast"
          : undefined;
  const blockRe = /(:root|\.dark|\.high-contrast)\s*\{([^}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = blockRe.exec(css)) !== null) {
    const theme = nameOf(m[1]);
    if (!theme) continue;
    const tokRe = /--([\w-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%/g;
    let t: RegExpExecArray | null;
    while ((t = tokRe.exec(m[2])) !== null) {
      themes[theme][t[1]] = hslToRgb(
        parseFloat(t[2]),
        parseFloat(t[3]),
        parseFloat(t[4])
      );
    }
  }
  return themes;
}

function hslToRgb(h: number, s: number, l: number): Rgb {
  const sat = s / 100;
  const lig = l / 100;
  const c = (1 - Math.abs(2 * lig - 1)) * sat;
  const hp = (h % 360) / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  const table: [number, number, number][] = [
    [c, x, 0],
    [x, c, 0],
    [0, c, x],
    [0, x, c],
    [x, 0, c],
    [c, 0, x],
  ];
  const [r, g, b] = table[Math.floor(hp) % 6];
  const mAdj = lig - c / 2;
  return [r + mAdj, g + mAdj, b + mAdj];
}

function luminance([r, g, b]: Rgb): number {
  const chan = (v: number) =>
    v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b);
}

function contrastRatio(fg: Rgb, bg: Rgb): number {
  const l1 = luminance(fg);
  const l2 = luminance(bg);
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

const round2 = (v: number) => Math.round(v * 100) / 100;

// =============================================================================
// The pair contract
// =============================================================================

const PAIRS: ReadonlyArray<readonly [string, string]> = [
  // foreground family on base surfaces
  ["foreground", "background"],
  ["foreground", "card"],
  ["foreground", "muted"],
  ["card-foreground", "card"],
  ["popover-foreground", "popover"],
  // semantic X-foreground on X
  ["primary-foreground", "primary"],
  ["secondary-foreground", "secondary"],
  ["destructive-foreground", "destructive"],
  ["success-foreground", "success"],
  ["success-subdued-foreground", "success-subdued"],
  ["warning-foreground", "warning"],
  ["accent-foreground", "accent"],
  // muted-foreground on every surface it is drawn over
  ["muted-foreground", "muted"],
  ["muted-foreground", "card"],
  ["muted-foreground", "background"],
  ["muted-foreground", "popover"],
  // the outline Button's hover surface
  ["accent-foreground", "input"],
  // status hues as text on the surfaces they are painted on
  ["primary", "card"],
  ["primary", "background"],
  ["destructive", "card"],
  ["destructive", "background"],
  ["success", "card"],
  ["success", "background"],
  ["warning", "card"],
  ["warning", "background"],
];

const THEMES: readonly Theme[] = ["light", "dark", "high-contrast"];

describe("token-contrast guardrail — every text/background token pair meets WCAG AA (issue #777 AC7 / UI-ENH-12)", () => {
  const themes = parseThemes(readFileSync(cssPath, "utf-8"));

  it("all three theme blocks parsed with their core tokens present", () => {
    // Guard the parser itself: a token silently missing from a theme block
    // would otherwise drop that pair/theme from the matrix below.
    const required = [
      "foreground",
      "background",
      "card",
      "popover",
      "muted",
      "input",
      "primary",
      "secondary",
      "accent",
      "destructive",
      "success",
      "success-subdued",
      "warning",
    ];
    for (const theme of THEMES) {
      for (const tok of required) {
        expect(themes[theme][tok], `${theme} --${tok}`).toBeDefined();
      }
    }
  });

  it.each(PAIRS)("%s on %s is >= 4.5:1 in light, dark and high-contrast", (fg, bg) => {
    let min = Infinity;
    for (const theme of THEMES) {
      const fgRgb = themes[theme][fg];
      const bgRgb = themes[theme][bg];
      expect(fgRgb, `${theme} --${fg}`).toBeDefined();
      expect(bgRgb, `${theme} --${bg}`).toBeDefined();
      min = Math.min(min, contrastRatio(fgRgb, bgRgb));
    }
    expect(round2(min)).toBeGreaterThanOrEqual(4.5);
  });
});
