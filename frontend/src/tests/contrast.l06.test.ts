// frontend/src/tests/contrast.l06.test.ts
// Issue #777 (L06 / TQ-sibling-batch-03-07) — acceptance checks C1-C5: WCAG 2.x
// AA contrast for the shared design tokens as consumed by the bespoke
// component class strings. FROZEN acceptance check authored pre-fix —
// DISCRIMINATING: expected RED at base commit d5982bf5 with exactly these
// failing minima (verified against the token values in src/index.css):
//   C1  ui/button.tsx destructive variant — destructive-foreground label on
//       bg-destructive/90 at rest and bg-destructive at hover:      min 3.49
//       (dark hover; rest is 4.06 light / 4.10 dark, also below 4.5)
//   C2  AssistantMessage.tsx error body — text-destructive/80 composited on
//       bg-destructive/10 over --background:                        3.02 light / 3.27 dark
//   C3  ui/input.tsx + Composer.tsx placeholders
//       (muted-foreground/80 and /60 on --card):                    2.45 light (composer /60)
//   C4  ui/input.tsx + Composer.tsx focus border tint (the strongest
//       focus-visible:/focus-within: indicator, border-primary/50)
//       vs --card — WCAG 1.4.11 non-text minimum:                   2.03 light / 2.83 dark
//   C5  ui/tabs.tsx TabsList inactive label (muted-foreground on
//       --input):                                                   4.44 light
//
// Technique (mirrors target-size.audit.test.tsx's "compute, don't eyeball"
// approach): jsdom has no color engine, so the check parses the :root, .dark
// and .high-contrast HSL token blocks out of src/index.css with a regex,
// reads each component's class strings from its SOURCE FILE at runtime via
// node:fs — never a hardcoded copy of the classes — alpha-blends every /NN
// opacity modifier over the real surface token underneath (sRGB compositing:
// out = fg*a + bg*(1-a)) and computes the WCAG 2.x contrast ratio with the
// sRGB relative-luminance formula (channel v/12.92 if v<=0.04045 else
// ((v+0.055)/1.055)^2.4; L = 0.2126R + 0.7152G + 0.0722B;
// ratio = (L1+0.05)/(L2+0.05)). Each check asserts Math.round(min*100)/100
// across its theme/surface combinations so a failure reads
// "expected N.NN to be greater than or equal to 4.5" (or 3 for C4).
//
// Source anchors: every class string is located by a literal that must exist
// in the component source (bg-destructive/90, placeholder:text-muted-
// foreground/80, focus-within:border-primary/50, bg-input, ...). If a literal
// disappears the test throws "source anchor not found: <literal>" so a rename
// surfaces as RED, never as a vacuous green.
//
// The .high-contrast tokens are parsed too (the parser is generic), but per
// the issue the five checks assert light + dark only.

// Known extraction limits (documented #777 feedback, not silently green):
//   - extraction reads UNPREFIXED utilities only — a variant-scoped override
//     (dark:bg-*, hover:bg-*) of a measured token is invisible to these
//     checks; keep theme/hover overrides of measured tokens out of the
//     audited strings or extend the extractor deliberately;
//   - the surface/indicator helpers resolve FIRST match in source order,
//     not tailwind-merge last-wins — do not reorder measured strings;
//   - C4 models the ring against --card; with ring-offset-background the
//     offset band adjoins --background (both frames pass 3:1).
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// =============================================================================
// Token parsing — HSL triples from the src/index.css theme blocks
// =============================================================================

type Hsl = readonly [hue: number, sat: number, light: number]; // sat/light 0..1

const cssSource = readFileSync(resolve(__dirname, "../index.css"), "utf-8");

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function themeTokens(selector: string): Record<string, Hsl> {
  const blockRe = new RegExp(`(^|\\n)\\s*${escapeRegExp(selector)}\\s*\\{([^}]*)\\}`);
  const block = cssSource.match(blockRe);
  if (!block) throw new Error(`theme block not found in src/index.css: ${selector}`);
  const tokens: Record<string, Hsl> = {};
  for (const line of block[2].split("\n")) {
    const m = line.match(/--([a-z0-9-]+):\s*([0-9.]+)\s+([0-9.]+)%\s+([0-9.]+)%/);
    if (m) {
      tokens[m[1]] = [parseFloat(m[2]), parseFloat(m[3]) / 100, parseFloat(m[4]) / 100];
    }
  }
  return tokens;
}

const THEMES: Record<string, Record<string, Hsl>> = {
  light: themeTokens(":root"),
  dark: themeTokens(".dark"),
  "high-contrast": themeTokens(".high-contrast"),
};

/** The two themes the five checks assert (see header note on high-contrast). */
const CHECKED_THEMES = [THEMES.light, THEMES.dark];

function tokenRgb(theme: Record<string, Hsl>, name: string): [number, number, number] {
  const hsl = theme[name];
  if (!hsl) throw new Error(`token not found in theme: --${name}`);
  const [h, s, l] = hsl;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = (h / 60) % 2;
  const x = c * (1 - Math.abs(hp - 1));
  const m = l - c / 2;
  let rgb: [number, number, number];
  if (h < 60) rgb = [c, x, 0];
  else if (h < 120) rgb = [x, c, 0];
  else if (h < 180) rgb = [0, c, x];
  else if (h < 240) rgb = [0, x, c];
  else if (h < 300) rgb = [x, 0, c];
  else rgb = [c, 0, x];
  return [rgb[0] + m, rgb[1] + m, rgb[2] + m];
}

/** sRGB "simple alpha" compositing of a color token over a surface color. */
function blend(
  theme: Record<string, Hsl>,
  name: string,
  alpha: number,
  surface: readonly [number, number, number]
): [number, number, number] {
  const fg = tokenRgb(theme, name);
  return [
    fg[0] * alpha + surface[0] * (1 - alpha),
    fg[1] * alpha + surface[1] * (1 - alpha),
    fg[2] * alpha + surface[2] * (1 - alpha),
  ];
}

function luminance(rgb: readonly [number, number, number]): number {
  const ch = (v: number) => (v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2]);
}

function contrastRatio(
  fg: readonly [number, number, number],
  bg: readonly [number, number, number]
): number {
  const l1 = luminance(fg);
  const l2 = luminance(bg);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

/** Two-decimal rounding so the assertion message shows the canonical value. */
function round2(ratio: number): number {
  return Math.round(ratio * 100) / 100;
}

// =============================================================================
// Component source reading — class strings located by anchor literals
// =============================================================================

function readSource(relPath: string): string {
  return readFileSync(resolve(__dirname, relPath), "utf-8");
}

/** The double-quoted class string containing `literal`, read from component
 *  source at runtime. Throws if the literal is absent, so a class rename
 *  fails RED instead of silently checking a stale hardcoded copy. */
function classStringContaining(source: string, literal: string, file: string): string {
  if (!source.includes(literal)) {
    throw new Error(`source anchor not found in ${file}: ${literal}`);
  }
  const quoted = source.match(new RegExp(`"([^"\\n]*${escapeRegExp(literal)}[^"\\n]*)"`));
  if (!quoted) {
    throw new Error(`source anchor not found in ${file}: ${literal}`);
  }
  return quoted[1];
}

/** The EFFECTIVE class list of the cn(...) call containing `literal`:
 *  components split their class strings across several string literals
 *  (e.g. Composer's container keeps bg-card on one literal and the
 *  focus-within: tints on the next) and cn() merges them, so the tokens to
 *  inspect are their concatenation. Falls back to the single quoted string
 *  when the anchor is not inside a cn(...) call (plain className="..."
 *  attributes, cva variant tables). */
function cnClassString(source: string, literal: string, file: string): string {
  const anchor = source.indexOf(literal);
  if (anchor === -1) {
    throw new Error(`source anchor not found in ${file}: ${literal}`);
  }
  const cnStart = source.lastIndexOf("cn(", anchor);
  if (cnStart === -1) {
    return classStringContaining(source, literal, file);
  }
  let depth = 0;
  let end = -1;
  for (let i = cnStart; i < source.length; i++) {
    if (source[i] === "(") depth += 1;
    else if (source[i] === ")") {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end === -1) {
    throw new Error(`unbalanced cn(...) call around anchor in ${file}: ${literal}`);
  }
  const parts = source.slice(cnStart, end).match(/"([^"\n]*)"/g) ?? [];
  return parts.map((p) => p.slice(1, -1)).join(" ");
}

function tokensOf(classes: string): string[] {
  return classes.split(/\s+/).filter(Boolean);
}

function requireToken(classes: string, token: string, file: string): void {
  if (!tokensOf(classes).includes(token)) {
    throw new Error(`source anchor not found in ${file}: ${token}`);
  }
}

/** Alpha of a `/NN` opacity modifier (e.g. bg-destructive/90 -> 0.9;
 *  a bare token like hover:bg-destructive means full opacity, 1). */
function slashAlpha(token: string): number {
  const m = token.match(/\/(\d+)$/);
  return m ? parseInt(m[1], 10) / 100 : 1;
}

function findToken(classes: string, prefix: string): string | undefined {
  return tokensOf(classes).find((t) => t.startsWith(prefix));
}

/** Like findToken but the token's `/NN` alpha modifier is optional: matches
 *  `prefix` or `prefix/NN` and returns the alpha (1 when bare). C2/C3
 *  amendments (CHECK_WRONG, pre-implementation round 2): the planned fix
 *  removes the opacity modifiers, so the anchor must survive a bare token
 *  while still reading the alpha at base (3.02 / 2.45 RED there). */
function findTokenAlpha(classes: string, prefix: string, file: string): number {
  const token = tokensOf(classes).find(
    (t) => t === prefix || t.startsWith(prefix + "/")
  );
  if (!token) {
    throw new Error(`source anchor not found in ${file}: ${prefix}`);
  }
  return slashAlpha(token);
}

/** The surface token of the first un-prefixed `bg-*` utility in a class
 *  string (variant-prefixed and `/NN` stripped). C5 amendment (CHECK_WRONG,
 *  pre-implementation round 2): the TabsList surface token is derived from
 *  the source instead of pinned to bg-input, so the fix may move it (the AC
 *  says "the TabsList surface", not a specific token). Still RED at base:
 *  bg-input -> 4.44 light. */
function surfaceToken(classes: string, file: string): string {
  const token = tokensOf(classes).find((t) =>
    /^bg-[a-z][a-z0-9-]*(\/\d+)?$/.test(t)
  );
  if (!token) {
    throw new Error(`no surface bg-* token found in ${file}: ${classes}`);
  }
  return token.slice(3).replace(/\/\d+$/, "");
}

/** The strongest focus indicator among the `prefix`-prefixed tokens of a
 *  class string: a ring-<width> + ring-<color> pair beats a border-<color>
 *  tint; when only a border tint exists, use it. Returns the color token
 *  name and alpha, e.g. { color: "primary", alpha: 0.5 } for
 *  focus-visible:border-primary/50. */
function strongestFocusIndicator(
  classes: string,
  prefix: "focus-visible:" | "focus-within:",
  file: string
): { color: string; alpha: number } {
  const stripped = tokensOf(classes)
    .filter((t) => t.startsWith(prefix))
    .map((t) => t.slice(prefix.length));
  const split = (token: string): { color: string; alpha: number } => {
    const m = token.match(/^[a-z]+-(.+?)(?:\/(\d+))?$/);
    if (!m) throw new Error(`cannot parse indicator token in ${file}: ${token}`);
    return { color: m[1], alpha: m[2] ? parseInt(m[2], 10) / 100 : 1 };
  };
  // ring-0 removes the ring (zero width), so a width must be `ring` or ring-N
  // with N >= 1 to count as the stronger indicator.
  const hasRingWidth = stripped.some((t) => /^ring(-[1-9][0-9.]*)?$/.test(t));
  const ringColor = stripped.find((t) =>
    /^ring-(ring|primary|secondary|destructive|success|warning|muted|accent|border|input|background|foreground|card|popover)(\/\d+)?$/.test(
      t
    )
  );
  if (hasRingWidth && ringColor) return split(ringColor);
  const borderTint = stripped.find((t) => /^border-[a-z-]+(\/\d+)?$/.test(t));
  if (borderTint) return split(borderTint);
  throw new Error(`no ${prefix} indicator class found in ${file}: ${classes}`);
}

// =============================================================================
// The audit — five checks, RED at base d5982bf5
// =============================================================================

describe("contrast audit — shared design tokens meet WCAG AA (issue #777 / TQ-sibling-batch-03-07, C1-C5)", () => {
  it("destructive button label meets AA at rest and hover in both themes", () => {
    const file = "../components/ui/button.tsx";
    const source = readSource(file);
    // Anchor on the destructive cva variant string, then derive rest/hover
    // surfaces and the label color from its tokens — never a hardcoded copy.
    const variant = classStringContaining(source, "bg-destructive/90", "button.tsx");
    requireToken(variant, "text-destructive-foreground", "button.tsx");
    const restBg = findToken(variant, "bg-destructive");
    if (!restBg) throw new Error("source anchor not found in button.tsx: bg-destructive");
    const hoverBg = tokensOf(variant).find((t) => /^hover:bg-destructive(\/\d+)?$/.test(t));
    if (!hoverBg) throw new Error("source anchor not found in button.tsx: hover:bg-destructive");

    let min = Infinity;
    for (const theme of CHECKED_THEMES) {
      const background = tokenRgb(theme, "background");
      // rest: label on destructive@rest-alpha blended over --background
      min = Math.min(
        min,
        contrastRatio(
          tokenRgb(theme, "destructive-foreground"),
          blend(theme, "destructive", slashAlpha(restBg), background)
        )
      );
      // hover: label on destructive (full) over --background
      min = Math.min(
        min,
        contrastRatio(
          tokenRgb(theme, "destructive-foreground"),
          blend(theme, "destructive", slashAlpha(hoverBg), background)
        )
      );
    }
    expect(round2(min)).toBeGreaterThanOrEqual(4.5);
  });

  it("chat error body text meets AA in both themes", () => {
    const source = readSource("../components/chat/AssistantMessage.tsx");
    // The error block container and its body paragraph, located by literals.
    // (C2 amendment: body anchored on `text-xs text-destructive`, which the
    // fix keeps — only the /80 alpha is removed; alpha is optional below.)
    const container = classStringContaining(source, "bg-destructive/10", "AssistantMessage.tsx");
    const body = classStringContaining(source, "text-xs text-destructive", "AssistantMessage.tsx");
    const containerBg = findToken(container, "bg-destructive/");
    if (!containerBg) throw new Error("source anchor not found in AssistantMessage.tsx: bg-destructive/10");
    const bodyAlpha = findTokenAlpha(body, "text-destructive", "AssistantMessage.tsx");

    let min = Infinity;
    for (const theme of CHECKED_THEMES) {
      const background = tokenRgb(theme, "background");
      // Composited stack: container wash destructive@0.10 over --background,
      // body text destructive@bodyAlpha over that wash.
      const surface = blend(theme, "destructive", slashAlpha(containerBg), background);
      const fg = blend(theme, "destructive", bodyAlpha, surface);
      min = Math.min(min, contrastRatio(fg, surface));
    }
    expect(round2(min)).toBeGreaterThanOrEqual(4.5);
  });

  it("Input and composer placeholders meet AA in both themes", () => {
    // (C3 amendment: anchored on the bare placeholder token — a prefix of both
    // the base `/80`, `/60` forms and the fixed bare form; alpha optional.)
    const inputClasses = cnClassString(
      readSource("../components/ui/input.tsx"),
      "placeholder:text-muted-foreground",
      "input.tsx"
    );
    const composerSource = readSource("../components/chat/Composer.tsx");
    const composerTextarea = cnClassString(
      composerSource,
      "placeholder:text-muted-foreground",
      "Composer.tsx"
    );
    const inputAlpha = findTokenAlpha(
      inputClasses,
      "placeholder:text-muted-foreground",
      "input.tsx"
    );
    const composerAlpha = findTokenAlpha(
      composerTextarea,
      "placeholder:text-muted-foreground",
      "Composer.tsx"
    );
    // Both surfaces are --card: Input paints bg-card itself; the composer
    // textarea is transparent inside its bg-card container.
    requireToken(inputClasses, "bg-card", "input.tsx");
    const composerContainer = cnClassString(
      composerSource,
      "focus-within:border-primary/50",
      "Composer.tsx"
    );
    requireToken(composerContainer, "bg-card", "Composer.tsx");

    let min = Infinity;
    for (const theme of CHECKED_THEMES) {
      const card = tokenRgb(theme, "card");
      min = Math.min(
        min,
        contrastRatio(
          blend(theme, "muted-foreground", inputAlpha, card),
          card
        )
      );
      min = Math.min(
        min,
        contrastRatio(
          blend(theme, "muted-foreground", composerAlpha, card),
          card
        )
      );
    }
    expect(round2(min)).toBeGreaterThanOrEqual(4.5);
  });

  it("Input and composer focus indicators meet the non-text minimum in both themes", () => {
    const inputClasses = cnClassString(
      readSource("../components/ui/input.tsx"),
      "focus-visible:border-primary/50",
      "input.tsx"
    );
    const composerContainer = cnClassString(
      readSource("../components/chat/Composer.tsx"),
      "focus-within:border-primary/50",
      "Composer.tsx"
    );
    const inputIndicator = strongestFocusIndicator(inputClasses, "focus-visible:", "input.tsx");
    const composerIndicator = strongestFocusIndicator(composerContainer, "focus-within:", "Composer.tsx");

    let min = Infinity;
    for (const theme of CHECKED_THEMES) {
      const card = tokenRgb(theme, "card");
      min = Math.min(
        min,
        contrastRatio(blend(theme, inputIndicator.color, inputIndicator.alpha, card), card)
      );
      min = Math.min(
        min,
        contrastRatio(blend(theme, composerIndicator.color, composerIndicator.alpha, card), card)
      );
    }
    expect(round2(min)).toBeGreaterThanOrEqual(3);
  });

  it("inactive tab labels meet AA in both themes", () => {
    // (C5 amendment: anchored on the TabsList shape (`inline-flex h-10`),
    // surface token derived dynamically — RED at base via bg-input 4.44,
    // GREEN after the surface moves to bg-muted 5.25/5.53. The AC says
    // "the TabsList surface", not a specific token.)
    const tabsList = classStringContaining(
      readSource("../components/ui/tabs.tsx"),
      "inline-flex h-10",
      "tabs.tsx"
    );
    requireToken(tabsList, "text-muted-foreground", "tabs.tsx");
    const surface = surfaceToken(tabsList, "tabs.tsx");

    let min = Infinity;
    for (const theme of CHECKED_THEMES) {
      // Inactive TabsTrigger labels inherit the TabsList muted-foreground on
      // the derived surface token (no alpha modifier on the label token).
      min = Math.min(
        min,
        contrastRatio(tokenRgb(theme, "muted-foreground"), tokenRgb(theme, surface))
      );
    }
    expect(round2(min)).toBeGreaterThanOrEqual(4.5);
  });
});
