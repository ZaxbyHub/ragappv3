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
// #777 token moves. The COMPOSITE_PAIRS section additionally asserts the
// alpha-composited states components actually paint (token x /NN modifier
// blended over its real under-surface) — full-opacity pairs alone cannot see
// those, which is exactly how the RejectedFilesBanner dark regression and the
// Badge hover regression slipped the first cut of this file (#777 feedback).
// Direction caveat the first cut got wrong: "an alpha modifier over a lighter
// surface can only lower contrast" holds for light themes, but a DARK theme
// wash is barely lighter than the background, so a dark foreground's ratio
// can move either way — composited pairs must be measured, not inferred.
//
// Pair scope (25 full-opacity pairs + composited states, x 3 themes):
//   - every X-foreground token on its own X surface (the system's pairing
//     convention), plus foreground-family pairs on their base surfaces;
//   - muted-foreground on every solid surface it is drawn over (muted, card,
//     background, popover);
//   - accent-foreground on input (the outline Button's hover surface);
//   - the status hues (primary/destructive/success/warning) as TEXT on card
//     and background, the surfaces they are actually painted on;
//   - composited: destructive text on its own /10 wash over background (the
//     chat error bubble), warning-foreground on warning/95 over background
//     (ReconnectingBanner), destructive-foreground on destructive/90 over
//     card (Badge hover).
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
//      warning 4.64, dark destructive 4.73 (over card); WikiPageList bulk bar 5.5+.
//   4. muted-foreground on --accent HOVER surfaces (4.48 light / 3.37 dark /
//      2.11 high-contrast) and foreground on --accent in high-contrast
//      (3.50): pre-existing hover-only consumers (DraftClaimsPanel:279,
//      DraftFindingsPanel:137 pills; Composer hover chips) outside #777's
//      six findings; the tabs consumer this PR created was fixed by making
//      the count spans inherit the trigger color. Needs a light/dark
//      --accent token decision — follow-up, not silently green here.
//   5. (CLOSED by the #778 review) the warning-tint text family in LIGHT
//      theme moved with --warning (33% -> 28%) and is now pinned in
//      COMPOSITE_PAIRS (warning on warning/10 over background); the success
//      /10 family joined it when --success moved 30% -> 26%.

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

// Composited states components actually paint: [text token, wash hue token,
// wash alpha, under-surface token, label]. The rendered surface is
// wash x alpha blended over the under-surface. All values verified >= 4.5 in
// all three themes with the #777 tokens (see the header for the states that
// CANNOT be pinned yet — exclusions 4 and 5).
const COMPOSITE_PAIRS: ReadonlyArray<readonly [string, string, number, string, string]> = [
  // the chat error bubble (AssistantMessage/MessageBubble): destructive text
  // on the bg-destructive/10 wash over the page background
  ["destructive", "destructive", 0.1, "background", "destructive text on destructive/10 over background"],
  // ReconnectingBanner degraded mode: warning-foreground on bg-warning/95
  ["warning-foreground", "warning", 0.95, "background", "warning-foreground on warning/95 over background"],
  // destructive Badge hover (badge.tsx): label on destructive/90 over card
  ["destructive-foreground", "destructive", 0.9, "card", "destructive-foreground on destructive/90 over card"],
  // issue #778 review F-04: the light /10 wash families the token moves fixed.
  // success/10 badges (WikiPageList "verified", draft "Ready") and warning/10
  // alerts over background — sub-AA (4.02 / 4.12) at the pre-review token
  // values, passing at the moved values.
  ["success", "success", 0.1, "background", "success text on success/10 over background"],
  ["warning", "warning", 0.1, "background", "warning text on warning/10 over background"],
];

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

  it.each(COMPOSITE_PAIRS)(
    "%s on %s/%s over %s is >= 4.5:1 in light, dark and high-contrast",
    (fg, wash, alpha, under, label) => {
      let min = Infinity;
      for (const theme of THEMES) {
        const fgRgb = themes[theme][fg];
        const washRgb = themes[theme][wash];
        const underRgb = themes[theme][under];
        expect(fgRgb, `${theme} --${fg}`).toBeDefined();
        expect(washRgb, `${theme} --${wash}`).toBeDefined();
        expect(underRgb, `${theme} --${under}`).toBeDefined();
        // the rendered surface: wash hue x alpha over the under-surface
        const surface = washRgb.map(
          (v, i) => v * alpha + underRgb[i] * (1 - alpha)
        ) as Rgb;
        min = Math.min(min, contrastRatio(fgRgb, surface));
      }
      expect(round2(min), label).toBeGreaterThanOrEqual(4.5);
    }
  );
});
